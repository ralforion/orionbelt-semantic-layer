---
description: "Python API for OrionBelt's SemanticValidator: the checks a model passes before it can be loaded, and the diagnostics it emits."
---

# Semantic Validation

Validation runs after parsing and before a model is usable, so a broken model fails at load time rather than at query time.

## Semantic Validator

::: orionbelt.parser.validator.SemanticValidator
    options:
      show_source: true
      members:
        - validate
