---
description: "Python API for the OrionBelt compilation pipeline: the phase driver, the star-schema planner, the join graph, and the SQL code generator."
---

# Compilation Pipeline

The driver that takes a `QueryObject` through resolution, planning and generation, plus the star-schema planner, join graph and code generator it calls.

## Compiler Pipeline

::: orionbelt.compiler.pipeline.CompilationPipeline
    options:
      show_source: true
      members:
        - compile

## Star Schema Planner

::: orionbelt.compiler.star.StarSchemaPlanner
    options:
      show_source: true
      members:
        - plan

## CFL Planner

::: orionbelt.compiler.cfl.CFLPlanner
    options:
      show_source: true
      members:
        - plan

## Join Graph

::: orionbelt.compiler.graph.JoinGraph
    options:
      show_source: true
      members:
        - find_join_path
        - build_join_condition
        - detect_cycles

## Code Generator

::: orionbelt.compiler.codegen.CodeGenerator
    options:
      show_source: true
      members:
        - generate
