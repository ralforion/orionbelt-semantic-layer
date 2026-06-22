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

### Compiler passes (planned)

Feature wrappers (filter context, period-over-period, totals, cumulative, window,
and `having` projection cleanup) currently compose through ordered logic inside
`CompilationPipeline`. The [architecture improvement plan](#architecture-guardrails)
introduces an explicit *compiler pass* model so that pass ordering is declared once
and incompatible feature combinations are reported from a single compatibility
function, without changing the public `CompilationPipeline.compile()` behaviour or
the generated SQL. This section will be expanded with the pass contract once that
work lands.

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
