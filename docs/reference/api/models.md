---
description: "Python API for OrionBelt's core data structures: the semantic model with its dimensions, measures and metrics; the QueryObject and its parts; and the error and validation types."
---

# Model and Query Objects

The objects you build, pass around, and get back: the model itself, the query against it, and the errors when something does not line up.

## Semantic Model

::: orionbelt.models.semantic.SemanticModel
    options:
      show_source: true

::: orionbelt.models.semantic.DataObject
    options:
      show_source: true

::: orionbelt.models.semantic.Dimension
    options:
      show_source: true

::: orionbelt.models.semantic.Measure
    options:
      show_source: true

::: orionbelt.models.semantic.Metric
    options:
      show_source: true

## Query Models

::: orionbelt.models.query.QueryObject
    options:
      show_source: true

::: orionbelt.models.query.QuerySelect
    options:
      show_source: true

::: orionbelt.models.query.QueryFilter
    options:
      show_source: true

::: orionbelt.models.query.UsePathName
    options:
      show_source: true

::: orionbelt.models.query.DimensionRef
    options:
      show_source: true

## Error Models

::: orionbelt.models.errors.SemanticError
    options:
      show_source: true

::: orionbelt.models.errors.ValidationResult
    options:
      show_source: true

::: orionbelt.models.errors.SourceSpan
    options:
      show_source: true
