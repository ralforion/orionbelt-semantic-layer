---
description: "Python API for OrionBelt's service layer: ModelStore loads, describes and compiles against semantic models; SessionManager owns session lifecycle and per-session model stores."
---

# Service Layer

The entry point for embedding OrionBelt in a Python process: load models into a store, then compile queries against them.

## ModelStore

::: orionbelt.service.model_store.ModelStore
    options:
      show_source: true
      members:
        - load_model
        - get_model
        - describe
        - list_models
        - remove_model
        - compile_query
        - validate

## SessionManager

::: orionbelt.service.session_manager.SessionManager
    options:
      show_source: true
      members:
        - start
        - stop
        - create_session
        - get_store
        - get_session
        - close_session
        - list_sessions
        - active_count
        - get_or_create_default

## SessionInfo

::: orionbelt.service.session_manager.SessionInfo
    options:
      show_source: true
