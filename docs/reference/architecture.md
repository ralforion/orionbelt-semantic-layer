# Architecture

OrionBelt compiles YAML semantic models into dialect-specific SQL through a multi-phase pipeline.

## Compilation Pipeline

```
YAML Model          Query Object
    |                    |
    v                    v
 ┌───────────┐    ┌──────────────┐
 │  Parser   │    │  Resolution  │  ← Phase 1: resolve refs, select fact table,
 │  (ruamel) │    │              │    find join paths, classify filters
 └────┬──────┘    └──────┬───────┘
      │                  │
      v                  v
 SemanticModel    ResolvedQuery
      │                  │
      │    ┌─────────────┘
      │    │
      v    v
 ┌───────────────┐
 │   Planner     │  ← Phase 2: Star Schema or CFL (multi-fact)
 │  (star / cfl) │    builds SQL AST with joins, grouping, CTEs
 └───────┬───────┘
         │
         v
    SQL AST (Select, Join, Expr...)
         │
         v
 ┌───────────────┐
 │   Codegen     │  ← Phase 3: dialect renders AST to SQL string
 │  (dialect)    │    handles quoting, time grains, functions
 └───────┬───────┘
         │
         v
    SQL String (dialect-specific)
```

## Key Components

- **Parser** (`parser/`) — ruamel.yaml loader with source position tracking for error reporting
- **Resolution** (`compiler/resolution.py`) — selects the base data object (fact table), resolves dimension/measure references, determines join paths, classifies filters
- **Planner** — two strategies:
    - **Star Schema** (`compiler/star.py`) — single-fact queries with LEFT JOINs
    - **CFL** (`compiler/cfl.py`) — multi-fact Composite Fact Layer using UNION ALL + NULL padding
- **Codegen** (`compiler/codegen.py` + `dialect/`) — renders the SQL AST to a dialect-specific SQL string
- **Validator** (`compiler/validator.py`) — post-generation sqlglot syntax check (non-blocking warnings)

The pipeline is orchestrated by `CompilationPipeline` in `compiler/pipeline.py`. See the [Compilation Pipeline guide](../guide/compilation.md) for details.

### Compiler passes

After planning, aggregate-mode queries run through a fixed sequence of AST
transformations (the "passes"), defined in `compiler/passes.py`:

| Order | Pass | Applies when |
| --- | --- | --- |
| 1 | `filter_context` | a measure declares a filter-context override |
| 2 | `period_over_period` | a selected metric is period-over-period |
| 3 | `totals` | a measure uses `total` / grain override |
| 4 | `cumulative` | a selected metric is cumulative |
| 5 | `window` | a selected metric is a window metric (rank/lag/lead/...) |
| 6 | `having_projection_cleanup` | HAVING auto-included a measure not in `select` |

Each pass is a frozen `CompilerPass` (a `name`, an `applies` predicate, a
`run(ast, ctx)` callable, and `incompatible_with` metadata). The order is
load-bearing and declared once in `build_default_passes()`; `CompileContext`
carries the shared resolution/model/dialect inputs. Cross-feature
compatibility rules live in a single `evaluate_compatibility()` function that
returns structured warnings plus the set of passes to suppress (for example,
`totals` is suppressed and a warning recorded when combined with
period-over-period or cumulative metrics, because totals rewrites the AST those
wrappers depend on). The public `CompilationPipeline.compile()` behaviour, the
generated SQL, and the explain flags are unchanged by this structure.

## Architecture guardrails

An informational **architecture inventory** runs as part of the test suite
(`tests/architecture/`). It records, from the source tree:

- the largest modules (the concentration points most likely to accrue unrelated concerns),
- import cycles inside `src/orionbelt` (currently none),
- `RawSQL` construction sites (the dialect escape hatch we want to keep narrow and visible),
- broad `except` sites outside the approved boundary modules (HTTP, wire protocols,
  caches, DB drivers, and the YAML parser are expected to catch broadly; the core
  compiler/dialect/model layers are not).

The inventory is a measurement baseline only: it prints a stable, sorted report in
the test session summary and does not fail CI on the shape of the codebase. Later
phases of the architecture program promote individual measurements (coverage,
dependency direction, `RawSQL` count) into enforced gates.
