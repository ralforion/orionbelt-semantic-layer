# ob-duckdb — OrionBelt Semantic Layer Driver for DuckDB

## Purpose

PEP 249 DB-API 2.0 driver wrapping `duckdb` that intercepts OBML YAML
queries, compiles them to DuckDB SQL via the OrionBelt CompilationPipeline
(direct import) or OB REST API (standalone), and executes them natively.

**OB dialect string:** `"duckdb"`
**Author:** Ralf Becher / RALFORION d.o.o. (info@orionbelt.ai)
**License:** Apache 2.0

---

## Why DuckDB is Special

DuckDB is unique among the OB-supported vendors in two ways:

1. **In-process** — DuckDB runs embedded in the same Python process, no
   server required. This makes it the ideal driver for local development,
   testing, and notebook use without any infrastructure.

2. **Already has a DB-API 2.0 interface** — `duckdb.connect()` returns a
   PEP 249-compliant connection directly. This driver is a thin OBML-aware
   wrapper, not a full reimplementation.

DuckDB is also the recommended backend for **testing all other OB drivers**
— load a Parquet or CSV file into DuckDB, point the OB model at it, compile
with dialect="duckdb", and validate query output without needing Snowflake
or Postgres credentials.

---

## Module Map

| File | Responsibility |
|---|---|
| `__init__.py` | PEP 249 connect() + module constants |
| `connection.py` | Connection class wrapping duckdb.DuckDBPyConnection |
| `cursor.py` | Cursor class — OBML detection + execution |
| `compiler.py` | Direct OB import or REST fallback (shared pattern) |
| `exceptions.py` | PEP 249 exception hierarchy |
| `type_codes.py` | PEP 249 type objects |

---

## connect() Parameters

### DuckDB-specific
| Parameter | Type | Description |
|---|---|---|
| database | str | Path to .duckdb file, or `:memory:` (default) |
| read_only | bool | Open in read-only mode (default: False) |
| config | dict | DuckDB config options (threads, memory_limit, etc.) |

### OrionBelt parameters (same across all vendors)
| Parameter | Default | Description |
|---|---|---|
| ob_model_id | None | Pre-loaded OB model ID |
| ob_model_yaml | None | OBML YAML string to load |
| ob_model_file | None | Path to .obml.yaml file |
| ob_api_url | http://localhost:8000 | OB REST API URL (standalone mode only) |
| ob_timeout | 30 | HTTP timeout in seconds |

---

## Key DuckDB-Specific Notes

- `duckdb.connect()` default is `:memory:` — in-memory database, ideal for
  testing and local development with Parquet/CSV sources
- DuckDB supports reading Parquet, CSV, JSON directly in SQL:
  `SELECT * FROM 'data.parquet'` — OBML models can reference these as
  data objects with `code: 'path/to/file.parquet'`
- No server process — commit() and rollback() are supported but DuckDB
  auto-commits by default in most contexts
- DuckDB's DB-API cursor `.description` uses duckdb type names (VARCHAR,
  INTEGER, DOUBLE, TIMESTAMP, etc.) — map to PEP 249 type codes accordingly
- DuckDB is the best choice for the `ob_model_file` pattern: load a model
  pointing at local Parquet files, query with OBML, zero infrastructure
- Thread safety: DuckDB connections are NOT thread-safe by default.
  Use `duckdb.connect(database, check_same_thread=False)` for multi-threaded
  use, or create one connection per thread.
- DuckDB dialect in OB generates standard ANSI SQL with DuckDB extensions
  for time grains: `DATE_TRUNC('month', col)`, `EXTRACT(year FROM col)`

---

## Ideal Use Cases

1. **Local development & testing** — validate OBML models against sample
   data without any cloud credentials
2. **CI/CD pipelines** — unit test semantic models with DuckDB + Parquet
   fixtures, fast and dependency-free
3. **Notebooks / data science** — OBML queries against local files in Jupyter
4. **ob-flight-extension testing** — spin up a Flight SQL server backed by
   DuckDB for end-to-end driver testing without cloud access

---

## Example Usage

```python
import ob_duckdb

# In-memory with local Parquet files
conn = ob_duckdb.connect(
    database=":memory:",
    ob_model_file="orionbelt_1_model.obml.yaml",
)

with conn.cursor() as cur:
    cur.execute("""
select:
  dimensions:
    - Region
  measures:
    - Revenue
limit: 10
""")
    print(cur.fetchall())
```

---

## Compiler Bridge

Same auto-detect pattern as all other vendor drivers:

```python
def compile_obml(obml, *, model, dialect="duckdb", **kwargs) -> str:
    try:
        from orionbelt.compiler.pipeline import CompilationPipeline
        return CompilationPipeline().compile(obml, model, "duckdb").sql
    except ImportError:
        return _compile_rest(obml, dialect="duckdb", **kwargs)
```

---

## Type Mapping

| DuckDB type | PEP 249 type code |
|---|---|
| INTEGER, BIGINT, HUGEINT | NUMBER |
| FLOAT, DOUBLE, DECIMAL | NUMBER |
| VARCHAR, TEXT | STRING |
| BOOLEAN | STRING |
| DATE | DATETIME |
| TIMESTAMP, TIMESTAMPTZ | DATETIME |
| BLOB | BINARY |
| LIST, STRUCT, MAP | STRING (JSON repr) |

---

## Build Order

Session 1: exceptions.py + type_codes.py + compiler.py + unit tests
Session 2: connection.py + cursor.py (thin wrapper over duckdb native)
Session 3: __init__.py + connect() + integration tests with :memory: DB
Session 4: SQLAlchemy dialect (ob+duckdb:// URL scheme)

---

## Dependencies

```toml
[project.dependencies]
duckdb = ">=1.0"
pyyaml = ">=6.0"
httpx = ">=0.27"

[project.optional-dependencies]
sqlalchemy = ["sqlalchemy>=2.0"]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "mypy>=1.10", "ruff>=0.4"]
```

Note: no `respx` needed for unit tests — DuckDB in-memory means integration
tests run without any external service, so the unit/integration distinction
is less relevant than for other vendors.
