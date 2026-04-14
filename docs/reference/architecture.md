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
