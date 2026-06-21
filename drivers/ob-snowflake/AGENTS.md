# ob-snowflake — OrionBelt Semantic Layer Driver for Snowflake

## Purpose

PEP 249 DB-API 2.0 driver wrapping `snowflake-connector-python = ">=3.0"` that intercepts OBML YAML
queries, compiles them to snowflake SQL via the OrionBelt CompilationPipeline
(direct import) or OB REST API (standalone), and executes them natively.

**OB dialect string:** `"snowflake"`
**Author:** Ralf Becher / RALFORION d.o.o. (info@orionbelt.ai)
**License:** Apache 2.0

---

## Module Map

| File | Responsibility |
|---|---|
| `__init__.py` | PEP 249 connect() + module constants |
| `connection.py` | Connection class |
| `cursor.py` | Cursor class — OBML detection + execution |
| `compiler.py` | Direct OB import or REST fallback |
| `exceptions.py` | PEP 249 exception hierarchy |
| `type_codes.py` | PEP 249 type objects |

---

## connect() Parameters

### snowflake-specific
| account | str | Snowflake account identifier (e.g. xy12345.eu-west-1) |
| user | str | Snowflake username |
| password | str | Snowflake password |
| database | str | Default database |
| schema | str | Default schema |
| warehouse | str | Virtual warehouse |
| role | str | Snowflake role |

### OrionBelt parameters (same across all vendors)
| Parameter | Default | Description |
|---|---|---|
| ob_model_id | None | Pre-loaded OB model ID |
| ob_model_yaml | None | OBML YAML string to load |
| ob_model_file | None | Path to .obml.yaml file |
| ob_api_url | http://localhost:8000 | OB REST API URL (standalone mode only) |
| ob_timeout | 30 | HTTP timeout in seconds |

---

## Compiler Bridge (compiler.py)

Auto-detects whether OB core is importable (same process as orionbelt-api)
or whether to fall back to REST:

```python
def compile_obml(obml: dict, model, dialect: str) -> str:
    try:
        # Direct call — zero overhead, used when embedded in orionbelt-api
        from orionbelt.compiler.pipeline import CompilationPipeline
        result = CompilationPipeline().compile(obml, model, dialect)
        return result.sql
    except ImportError:
        # Standalone mode — call REST API
        return _compile_via_rest(obml, dialect)
```

---

## Vendor-Specific Notes

- Auto-commit is ON by default in Snowflake; commit() is a no-op unless autocommit=False
- executemany() with OBML raises NotSupportedError
- Use TIMESTAMP_NTZ for timezone-naive datetimes in generated SQL
- Snowflake identifiers are UPPERCASE by default; OB handles quoting via sqlglot

---

## Type System

Use snowflake.connector.cursor().description for column metadata.
Types map via FIELD_TYPES dict in snowflake.connector.constants.

---

## Build Order

Session 1: exceptions.py + type_codes.py + compiler.py + unit tests
Session 2: connection.py + cursor.py (OBML detection, core execute)
Session 3: __init__.py + connect() + integration tests
Session 4: SQLAlchemy dialect (ob+snowflake:// URL scheme)

---

## Dependencies

```toml
[project.dependencies]
snowflake-connector-python = ">=3.0"
pyyaml = ">=6.0"

[project.optional-dependencies]
sqlalchemy = ["sqlalchemy>=2.0"]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "mypy>=1.10", "ruff>=0.4", "respx>=0.21"]
```
