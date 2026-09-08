---
description: "Python API for the OrionBelt REST surface: request and response schemas for sessions, models, queries and validation, plus the Settings object that configures a runtime."
---

# API Schemas and Settings

The request and response shapes the REST API speaks, and the settings that configure a running instance.

## API Schemas

::: orionbelt.api.schemas
    options:
      show_source: true
      members:
        - SessionCreateRequest
        - SessionResponse
        - SessionListResponse
        - ModelLoadRequest
        - ModelLoadResponse
        - ModelSummaryResponse
        - SessionQueryRequest
        - QueryCompileResponse
        - ValidateRequest
        - ValidateResponse
        - DialectListResponse
        - HealthResponse

## Settings

::: orionbelt.settings.Settings
    options:
      show_source: true
