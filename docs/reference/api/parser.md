---
description: "Python API for OrionBelt's YAML front end: the tracked loader that keeps source positions through parsing, and the reference resolver that binds names across files."
---

# Parser

The parser turns OBML YAML into a semantic model, keeping source spans so errors point back at the line that caused them.

## YAML Parser

::: orionbelt.parser.loader.TrackedLoader
    options:
      show_source: true
      members:
        - load
        - load_string
        - load_model_directory

## Reference Resolver

::: orionbelt.parser.resolver.ReferenceResolver
    options:
      show_source: true
      members:
        - resolve
