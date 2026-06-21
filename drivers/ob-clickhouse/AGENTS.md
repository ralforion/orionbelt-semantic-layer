# ob-clickhouse — OrionBelt Semantic Layer Driver for Clickhouse

## Purpose

PEP 249 DB-API 2.0 driver wrapping `clickhouse-connect = ">=0.7"` that intercepts OBML YAML
queries, compiles them to clickhouse SQL via the OrionBelt CompilationPipeline
(direct import) or OB REST API (standalone), and executes them natively.

**OB dialect string:** `"clickhouse"`
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

### clickhouse-specific
| host | str | ClickHouse host |
| port | int | HTTP port (default: 8123) or native (9000) |
| username | str | Username (default: default) |
| password | str | Password |
| database | str | Database name |
| secure | bool | Use HTTPS (default: False) |

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

- ClickHouse has no transactions; commit() and rollback() are no-ops
- Use clickhouse_connect.get_client() not legacy clickhouse-driver
- FixedString padding: strip trailing null bytes in _post_process_rows()
- OB dialect "clickhouse" generates toStartOfMonth() etc. for time grains
- Arrays and Maps are not supported in OBML measures — raise DataError if encountered

---

## Type System

clickhouse-connect returns Python native types.
Map: Int32/64→int, Float32/64→float, String→str, DateTime→datetime.
FixedString columns require TRIM() — add post-processing in cursor.fetchall().

---

## Build Order

Session 1: exceptions.py + type_codes.py + compiler.py + unit tests
Session 2: connection.py + cursor.py (OBML detection, core execute)
Session 3: __init__.py + connect() + integration tests
Session 4: SQLAlchemy dialect (ob+clickhouse:// URL scheme)

---

## Dependencies

```toml
[project.dependencies]
clickhouse-connect = ">=0.7"
pyyaml = ">=6.0"

[project.optional-dependencies]
sqlalchemy = ["sqlalchemy>=2.0"]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "mypy>=1.10", "ruff>=0.4", "respx>=0.21"]
```
