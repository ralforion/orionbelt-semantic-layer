# Gradio UI

OrionBelt includes an interactive web UI built with [Gradio](https://www.gradio.app/) for exploring and testing the compilation pipeline visually.

## Features

- **Side-by-side editors** — OBML model (YAML) and query (YAML) with syntax highlighting
- **Dialect selector** — Switch between all 8 supported SQL dialects
- **One-click compilation** — Compile button generates formatted SQL output
- **SQL validation feedback** — Warnings and validation errors from sqlglot are displayed as comments above the generated SQL
- **ER Diagram tab** — Visualize the semantic model as a Mermaid ER diagram with left-to-right layout, FK annotations, dotted lines for secondary joins, and an adjustable zoom slider
- **OSI Import / Export** — Import OSI format models (converted to OBML) and export OBML models to OSI format, with validation feedback
- **Dark / light mode** — Toggle via the header button; all inputs and UI state are persisted across mode switches

The bundled example model (`examples/sem-layer.obml.yml`) is loaded automatically on startup.

![SQL Compiler](../assets/ui-sqlcompiler-dark.png)

![ER Diagram](../assets/ui-er-diagram-dark.png)

The ER diagram is also available as download (MD or PNG) or via the REST API.

## Local Development

For local development, the Gradio UI is automatically mounted at `/ui` on the REST API server when the `ui` extra is installed:

```bash
uv sync --extra ui
uv run orionbelt-api
# -> API at http://localhost:8000
# -> UI  at http://localhost:8000/ui
```

## Standalone Mode

The UI can also run as a separate process, connecting to the API via `API_BASE_URL`:

```bash
# Start the REST API (required backend)
uv run orionbelt-api &

# Install UI deps and launch the Gradio UI (standalone on port 7860)
uv sync --extra ui
API_BASE_URL=http://localhost:8000 uv run orionbelt-ui
```

## Live Demo

The hosted demo is available at:

> **[http://35.187.174.102/ui](http://35.187.174.102/ui/?__theme=dark)**

API endpoint: `http://35.187.174.102` — Interactive docs: [Swagger UI](http://35.187.174.102/docs) | [ReDoc](http://35.187.174.102/redoc)
