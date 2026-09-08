---
description: "Python API for OrionBelt's SQL abstract syntax tree: the node types the compiler emits, and the builder that assembles a SELECT."
---

# SQL AST

OrionBelt generates SQL through a typed AST rather than string concatenation, which is what makes the output injection-safe by construction.

## SQL AST Nodes

::: orionbelt.ast.nodes.Select
    options:
      show_source: true

::: orionbelt.ast.nodes.ColumnRef
    options:
      show_source: true

::: orionbelt.ast.nodes.FunctionCall
    options:
      show_source: true

::: orionbelt.ast.nodes.BinaryOp
    options:
      show_source: true

::: orionbelt.ast.nodes.Literal
    options:
      show_source: true

## AST Builder

::: orionbelt.ast.builder.QueryBuilder
    options:
      show_source: true
