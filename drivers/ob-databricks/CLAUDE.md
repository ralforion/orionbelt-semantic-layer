# ob-databricks — OrionBelt Semantic Layer Driver for Databricks

## Purpose

PEP 249 DB-API 2.0 driver wrapping `databricks-sql-connector = ">=3.0"` that intercepts OBML YAML
queries, compiles them to databricks SQL via the OrionBelt CompilationPipeline
(direct import) or OB REST API (standalone), and executes them natively.

**OB dialect string:** `"databricks"`
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

### databricks-specific
| server_hostname | str | Databricks workspace hostname |
| http_path | str | SQL warehouse HTTP path |
| access_token | str | Personal access token or M2M OAuth token |
| catalog | str | Unity Catalog name (default: hive_metastore) |
| schema | str | Schema name |

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

- databricks-sql-connector uses %s paramstyle
- Unity Catalog three-part names: catalog.schema.table
- OB dialect "databricks" generates Databricks SQL syntax (DATEADD, DATEDIFF etc.)
- Access token auth preferred over OAuth for server-side enterprise installs
- HTTP path format: /sql/1.0/warehouses/<warehouse-id>
- commit() is a no-op; Databricks SQL warehouses are stateless

---

## Type System

Use databricks.sql.client cursor description for metadata.
Types: STRING→str, INT/BIGINT→int, DOUBLE/FLOAT→float, TIMESTAMP→datetime, BOOLEAN→bool.

---

## Build Order

Session 1: exceptions.py + type_codes.py + compiler.py + unit tests
Session 2: connection.py + cursor.py (OBML detection, core execute)
Session 3: __init__.py + connect() + integration tests
Session 4: SQLAlchemy dialect (ob+databricks:// URL scheme)

---

## Dependencies

```toml
[project.dependencies]
databricks-sql-connector = ">=3.0"
pyyaml = ">=6.0"

[project.optional-dependencies]
sqlalchemy = ["sqlalchemy>=2.0"]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "mypy>=1.10", "ruff>=0.4", "respx>=0.21"]
```
