# ob-bigquery - OrionBelt Semantic Layer Driver for BigQuery

## Purpose

PEP 249 DB-API 2.0 driver wrapping `google-cloud-bigquery[pandas] >= 3.20`
that intercepts OBML YAML queries, compiles them to BigQuery SQL via the
OrionBelt REST API, and executes them natively.

Unlike several other drivers, this package delegates OBML detection and REST
compilation to `ob-driver-core`; it does not use a direct in-process
`CompilationPipeline` bridge.

**OB dialect string:** `"bigquery"`
**Author:** Ralf Becher / RALFORION d.o.o. (info@orionbelt.ai)
**License:** BSL-1.1

---

## Module Map

| File | Responsibility |
|---|---|
| `__init__.py` | PEP 249 connect() + module constants |
| `connection.py` | Connection class wrapping `google.cloud.bigquery.Client` |
| `cursor.py` | Cursor class - OBML detection + execution + Arrow conversion |
| `compiler.py` | Re-export from `ob-driver-core` for OBML detection + REST compilation |
| `exceptions.py` | PEP 249 exception hierarchy |
| `type_codes.py` | PEP 249 type objects + BigQuery type mapping |

---

## connect() Parameters

### BigQuery-specific

| Parameter | Type | Description |
|---|---|---|
| `project` | str | GCP project ID; defaults to ADC project |
| `credentials` | object | Explicit Google auth credentials object |
| `credentials_file` | str | Path to service account JSON key file |
| `location` | str | Default dataset location, e.g. `US` or `EU` |

### OrionBelt parameters

| Parameter | Default | Description |
|---|---|---|
| `ob_api_url` | `http://localhost:8000` | OrionBelt REST API URL; must be running in single-model mode |
| `ob_timeout` | 30 | HTTP timeout in seconds |

---

## Vendor-Specific Notes

- BigQuery auth uses Application Default Credentials unless `credentials` or
  `credentials_file` is provided.
- `paramstyle` is `pyformat`; named parameters are converted to
  `ScalarQueryParameter` values in `cursor.py`.
- `commit()` and `rollback()` are no-ops because BigQuery jobs auto-commit and
  this driver does not expose transactional state.
- `execute()` fetches results through `RowIterator.to_arrow()` and stores a
  PyArrow table for `fetch_arrow_table()`.
- `fetch_arrow_table()` consumes the stored Arrow table; subsequent calls raise
  `ProgrammingError`.
- OBML with `executemany()` raises `NotSupportedError`.
- `BOOLEAN`/`BOOL`, nested, geography, and JSON-like types are mapped to
  display-friendly PEP 249 type codes in `BQ_TYPE_MAP`.

---

## Dependencies

- `ob-driver-core>=0.1`
- `google-cloud-bigquery[pandas]>=3.20`
- `pyarrow>=16.0`

Development tooling is configured in this package's `pyproject.toml`:

```bash
uv run ruff check drivers/ob-bigquery/src
uv run mypy drivers/ob-bigquery/src
uv run pytest drivers/ob-bigquery
```
