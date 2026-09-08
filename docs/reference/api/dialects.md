---
description: "Python API for OrionBelt SQL dialects: the Dialect base class, the capability flags a dialect declares, and the registry that resolves a dialect by name."
---

# Dialects

A dialect declares what its target SQL engine can do; the registry maps a name to an implementation.

## Dialect Base

::: orionbelt.dialect.base.Dialect
    options:
      show_source: true

::: orionbelt.dialect.base.DialectCapabilities
    options:
      show_source: true

## Dialect Registry

::: orionbelt.dialect.registry.DialectRegistry
    options:
      show_source: true
      members:
        - get
        - available
        - register
