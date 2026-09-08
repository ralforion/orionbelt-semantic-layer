---
description: "Python API for the OrionBelt Composite Fact Layer planner: correct multi-fact, multi-grain queries with fan-trap and chasm-trap prevention."
---

# CFL Planner

The Composite Fact Layer planner handles queries that span separate fact tables at different grains without double counting.

## CFL Planner

::: orionbelt.compiler.cfl.CFLPlanner
    options:
      show_source: true
      members:
        - plan
