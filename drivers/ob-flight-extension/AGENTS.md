# ob-flight-extension — Arrow Flight SQL Server for orionbelt-api

## Purpose

Adds Arrow Flight SQL protocol to the existing orionbelt-api FastAPI server.
Runs as a background thread in the same Python process — no extra container,
no HTTP hop between Flight and the OB compilation engine.

Target deployment: on-premise Docker, LAN access, enterprise data teams.
Client tools: DBeaver, Tableau (via Flight JDBC .jar), Power BI (via ODBC bridge).

---

## Integration into orionbelt-api

### 1. Install as optional dependency

In orionbelt-api's pyproject.toml add:
```toml
[project.optional-dependencies]
flight = ["ob-flight-extension>=0.1", "pyarrow>=16.0"]
```

### 2. Hook into lifespan

In orionbelt/api/app.py, modify create_app():

```python
from contextlib import asynccontextmanager
import os

@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("FLIGHT_ENABLED", "false").lower() == "true":
        from ob_flight.startup import start_flight_background
        flight_thread = start_flight_background()
    yield
    if os.getenv("FLIGHT_ENABLED", "false").lower() == "true":
        from ob_flight.startup import stop_flight_server
        stop_flight_server()

app = FastAPI(lifespan=lifespan)
```

### 3. Docker port

Add port 8815 to docker-compose.yml (see ARCHITECTURE.md).

---

## Module Map

| File | Responsibility |
|---|---|
| startup.py | start_flight_background() — launches server in daemon thread |
| server.py | OBFlightServer(FlightServerBase) — pyarrow server subclass |
| handlers.py | GetFlightInfo, DoGet, GetSchema, ListFlights implementations |
| catalog.py | Model introspection → Flight catalog (tables = data objects) |
| auth.py | ServerAuthHandler — token or no-auth modes |
| converters.py | DB result rows → pyarrow RecordBatch |
| db_router.py | Routes to correct native connector based on model dialect |

---

## Key Implementation: GetFlightInfo

This is the entry point. DBeaver calls this first with the query text.

```python
def get_flight_info(self, context, descriptor):
    query_bytes = descriptor.command
    query_str = query_bytes.decode("utf-8")

    # Resolve model from descriptor path (= DBeaver "database" field)
    model_id = self._resolve_model(descriptor.path)
    model = self._model_cache[model_id]

    # Compile if OBML, pass through if plain SQL
    if is_obml(query_str):
        obml = yaml.safe_load(query_str)
        pipeline = CompilationPipeline()
        result = pipeline.compile(obml, model, dialect=model.dialect)
        sql = result.sql
    else:
        sql = query_str

    # Store sql+model in a short-lived ticket (UUID key → in-memory dict)
    ticket_id = str(uuid.uuid4())
    self._pending[ticket_id] = (sql, model_id)
    ticket = pa.flight.Ticket(ticket_id.encode())

    # Return schema + ticket (schema requires a dry-run or model metadata)
    schema = self._infer_schema(sql, model_id)
    endpoint = pa.flight.FlightEndpoint(ticket, [])
    return pa.flight.FlightInfo(schema, descriptor, [endpoint], -1, -1)
```

---

## Key Implementation: DoGet

DBeaver calls this after GetFlightInfo to stream the actual data.

```python
def do_get(self, context, ticket):
    ticket_id = ticket.ticket.decode("utf-8")
    sql, model_id = self._pending.pop(ticket_id)
    model = self._model_cache[model_id]

    # Execute on native connector
    conn = db_router.connect(model)
    cursor = conn.cursor()
    cursor.execute(sql)

    # Stream as Arrow record batches
    schema = pa.Schema.from_pandas(...)  # or from cursor.description
    def record_batch_generator():
        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break
            yield pa.RecordBatch.from_pydict(
                {col: [r[i] for r in rows]
                 for i, col in enumerate(col_names)},
                schema=schema,
            )
    return pa.flight.RecordBatchStream(
        pa.RecordBatchReader.from_batches(schema, record_batch_generator())
    )
```

---

## Key Implementation: ListFlights (DBeaver schema browser)

DBeaver calls ListFlights to populate the schema tree on the left panel.
Map OB model data objects → Flight "tables":

```python
def list_flights(self, context, criteria):
    for model_id, model in self._model_cache.items():
        for obj_name, obj in model.data_objects.items():
            descriptor = pa.flight.FlightDescriptor.for_path(
                model_id, obj_name
            )
            schema = self._schema_for_object(obj)
            yield pa.flight.FlightInfo(
                schema, descriptor, [], -1, -1
            )
```

This makes DBeaver show the OB model's data objects as browsable tables,
with correct column types visible before any query is run.

---

## db_router.py — Vendor Routing

Routes execution to the correct native connector based on the model's
declared dialect or an explicit env override:

```python
VENDOR_MAP = {
    "snowflake":  "ob_snowflake",
    "postgres":   "ob_postgres",
    "clickhouse": "ob_clickhouse",
    "dremio":     "ob_dremio",
    "databricks": "ob_databricks",
}

def connect(model: SemanticModel) -> Connection:
    dialect = model.dialect or os.getenv("DB_VENDOR", "snowflake")
    module = importlib.import_module(VENDOR_MAP[dialect])
    return module.connect(**_creds_from_env(dialect))
```

Credentials come from environment variables per vendor:
  SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD, ...
  POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, ...
  etc.

---

## Startup Thread

```python
import threading
import pyarrow.flight as flight

_server: flight.FlightServerBase | None = None

def start_flight_background() -> threading.Thread:
    global _server
    port = int(os.getenv("FLIGHT_PORT", "8815"))
    _server = OBFlightServer(location=f"grpc://0.0.0.0:{port}")
    _server.init()   # load models, init cache
    thread = threading.Thread(
        target=_server.serve,
        name="ob-flight-server",
        daemon=True,   # dies with the main process
    )
    thread.start()
    return thread

def stop_flight_server() -> None:
    if _server:
        _server.shutdown()
```

daemon=True is critical — ensures the Flight server thread dies cleanly
when the FastAPI process exits or is killed by Docker.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| FLIGHT_ENABLED | false | Set to "true" to activate |
| FLIGHT_PORT | 8815 | gRPC listen port |
| FLIGHT_AUTH_MODE | none | "none" or "token" |
| FLIGHT_API_TOKEN | | Static token for auth mode "token" |
| FLIGHT_DEFAULT_MODEL | | Fallback model_id if path empty |
| FLIGHT_PRELOAD_MODELS | | Comma-sep .obml.yaml paths, loaded at startup |
| DB_VENDOR | snowflake | Default vendor for db_router |

**No `FLIGHT_ALLOW_*_SQL` flags.** Raw SQL and write operations are
rejected by design; there is no operator override. See
`design/PLAN_flight_natural_sql.md` §3.2.

---

## OrionBelt Semantic QL (OBSQL) (v2.4.0+)

**OrionBelt Semantic QL** — short form **OBSQL** — is OBSL's natural SQL
surface. The server exposes each model as **one virtual table** named
`<model_id>` with columns = dimensions + measures + metrics. BI tools
(Tableau, Power BI, DBeaver) browse this table and write Semantic QL
against it:

```sql
SELECT "Region", "Total Sales"
FROM   sales_model
WHERE  "Year" = 2025
ORDER  BY "Total Sales" DESC
LIMIT  100
```

`server._classify_sql(sql, model)` parses the SQL and dispatches to one
of three modes — there are **no escape-hatch env flags**:

| Shape | Mode | Path |
|---|---|---|
| `FROM <model_id>` (the virtual table) | `semantic` | `translate_sql_to_query` → `CompilationPipeline.compile` → warehouse |
| `SHOW TABLES`, `DESCRIBE`, `information_schema.*`, `pg_catalog.*`, `SELECT 1`, `SELECT version()` | `catalog` | `_handle_catalog_sql` → model-backed `pa.Table`; **never touches warehouse** |
| anything else (raw FROM, data-object labels, multi-statement) | `rejected` | `RAW_SQL_REJECTED` |
| `INSERT`/`UPDATE`/`DELETE`/`DROP`/`CREATE`/`ALTER`/`TRUNCATE`/`MERGE`/… | `rejected` | `WRITE_OPERATION_REJECTED` (checked before classification, applies to every path) |

Semantic-mode queries also benefit from a **schema probe shortcut**: the
result `pa.Schema` is built from the `QueryObject` + model metadata,
avoiding a warehouse round-trip on `GetFlightInfo`.

`WITH ROLLUP` / `WITH CUBE` (trailing modifier or `GROUP BY ROLLUP(...)`
function form) is supported end-to-end — see
`design/PLAN_with_rollup.md`.

---

## DBeaver Configuration (end-user guide)

1. New Connection → Apache Arrow Flight SQL
2. Host: <docker-host-ip>  Port: 8815
3. Database: <model_name_or_id>   (e.g. "orionbelt_1")
4. Authentication: leave empty (FLIGHT_AUTH_MODE=none on LAN)
5. Test Connection → should show "Connected"
6. In SQL editor, write either:
   - Plain SQL:  SELECT * FROM ORDERS LIMIT 10
   - OBML YAML:
       select:
         dimensions: [Region]
         measures: [Revenue]
7. Schema browser shows OB model data objects as tables

---

## Tableau Configuration (end-user guide)

1. Download flight-sql-jdbc-driver-*.jar from Apache Arrow releases
2. Tableau → Connect → Other Databases (JDBC)
3. URL: jdbc:arrow-flight-sql://<host>:8815?useEncryption=false
4. Username/Password: leave empty or use token
5. Custom SQL uses same OBML YAML or plain SQL

---

## Build Order

Session 1: converters.py + tests (rows → Arrow RecordBatch, no Flight needed)
Session 2: server.py + startup.py skeleton (pyarrow server, no handlers yet)
Session 3: handlers.py GetFlightInfo + DoGet (core query path)
Session 4: catalog.py + ListFlights (DBeaver schema browser)
Session 5: auth.py + db_router.py + integration test with DBeaver

---

## Dependencies

```toml
[project.dependencies]
pyarrow = ">=16.0"
pyyaml = ">=6.0"

[project.optional-dependencies]
snowflake = ["snowflake-connector-python>=3.0"]
postgres  = ["adbc-driver-postgresql>=1.0"]
clickhouse = ["clickhouse-connect>=0.7"]
dremio    = ["pyarrow>=16.0"]   # Dremio uses Arrow Flight natively
databricks = ["databricks-sql-connector>=3.0"]
all = [
    "snowflake-connector-python>=3.0",
    "adbc-driver-postgresql>=1.0",
    "clickhouse-connect>=0.7",
    "databricks-sql-connector>=3.0",
]
```
