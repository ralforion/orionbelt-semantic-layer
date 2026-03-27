# ob-dremio — OrionBelt Semantic Layer Driver for Dremio

## Purpose

PEP 249 DB-API 2.0 driver wrapping `pyarrow = ">=16.0"
pyarrow-hotfix = ">=0.6"` that intercepts OBML YAML
queries, compiles them to dremio SQL via the OrionBelt CompilationPipeline
(direct import) or OB REST API (standalone), and executes them natively.

**OB dialect string:** `"dremio"`
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

### dremio-specific
| host | str | Dremio host |
| port | int | Arrow Flight port (default: 32010) |
| username | str | Dremio username |
| password | str | Dremio password |
| schema | str | Space/schema path (e.g. "@user.myspace") |
| tls | bool | Use TLS (default: False for LAN) |

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

- Dremio connects via Arrow Flight (port 32010), NOT JDBC/ODBC
- Use pyarrow.flight.FlightClient with BasicAuth middleware for auth
- execute() sends query via do_get() with TicketStatementQuery
- This driver is unique: it IS already a Flight client internally
- COPY INTO SQL with German locale formatting was a prior pain point — always use . as decimal separator
- Space paths use dot notation: SELECT * FROM "myspace"."mytable"

---

## Type System

Dremio uses Arrow Flight natively — cursor maps Arrow schema directly.
pa.int32() → int, pa.float64() → float, pa.utf8() → str, pa.timestamp() → datetime.

---

## Build Order

Session 1: exceptions.py + type_codes.py + compiler.py + unit tests
Session 2: connection.py + cursor.py (OBML detection, core execute)
Session 3: __init__.py + connect() + integration tests
Session 4: SQLAlchemy dialect (ob+dremio:// URL scheme)

---

## Dependencies

```toml
[project.dependencies]
pyarrow = ">=16.0"
pyarrow-hotfix = ">=0.6"
pyyaml = ">=6.0"

[project.optional-dependencies]
sqlalchemy = ["sqlalchemy>=2.0"]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "mypy>=1.10", "ruff>=0.4", "respx>=0.21"]
```
