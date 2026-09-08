# ob-dremio — OrionBelt Semantic Layer Driver for Dremio

## Purpose

PEP 249 DB-API 2.0 driver wrapping `adbc-driver-flightsql` that intercepts
OBML YAML queries, compiles them to dremio SQL via the OrionBelt
CompilationPipeline (direct import) or OB REST API (standalone), and
executes them natively.

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
| db_kwargs | dict | Extra ADBC options (e.g. Dremio routing headers); merged last |
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

- Dremio connects via Arrow Flight SQL (port 32010), NOT JDBC/ODBC
- Dremio speaks Flight SQL natively, so the generic `adbc-driver-flightsql`
  **is** the Dremio driver — there is no vendor SDK in this path
- Auth is `AuthenticateBasicToken`, run by the driver: pass `username` /
  `password` as ADBC options and the bearer token is attached to every call.
  The old hand-rolled client called `authenticate_basic_token()` itself and
  threaded `FlightCallOptions` through every RPC
- `?` parameters bind as prepared-statement values. They could not before:
  the statement went out with placeholders intact and Dremio answered with a
  Calcite `RexDynamicParam` error
- **One native cursor per execution, never reused.** ADBC skips re-preparing
  when the SQL text is unchanged, and Dremio then answers the second
  execution with the *first* execution's rows even though new parameters
  were bound - silently, no error. The same reuse against OBSL's own Flight
  server rebinds correctly, which puts the fault on Dremio's side. This is
  also what the hand-rolled client effectively did: one `get_flight_info` +
  `do_get` per statement
- `executemany()` runs one statement per parameter set. ADBC's own
  `executemany` binds the batch over `DoPut`, which Dremio refuses with
  `acceptPut is not implemented`
- A **prepared** `COUNT` is refused: Dremio describes it as `int64` NOT NULL
  and streams it nullable, and ADBC compares the two. `SUM` / `MAX` and an
  unparameterised `COUNT` are fine, and OBSL only ever emits the latter
- `description` is built from the Arrow schema, not from the native cursor's
  own `description` — ADBC reports PyArrow `DataType` objects where PEP 249
  expects a type constant
- Dremio answers `GetSqlInfo` with an empty endpoint list, so
  `adbc_get_info()` raises and `adbc_get_objects()` returns no catalogs.
  Nothing in this driver calls them
- COPY INTO SQL with German locale formatting was a prior pain point — always use . as decimal separator
- Space paths use dot notation: SELECT * FROM "myspace"."mytable"

---

## Type System

Dremio uses Arrow Flight SQL natively — cursor maps Arrow schema directly.
The Arrow types are identical to what the hand-rolled Flight client
returned, verified against a live Dremio container: `date64[ms]`,
`timestamp[ms]`, `decimal128(18, 2)`, `int32`.
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
adbc-driver-flightsql = ">=1.0"
pyarrow = ">=16.0"
pyyaml = ">=6.0"

[project.optional-dependencies]
sqlalchemy = ["sqlalchemy>=2.0"]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "mypy>=1.10", "ruff>=0.4", "respx>=0.21"]
```
