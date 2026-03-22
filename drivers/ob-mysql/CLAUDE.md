# ob-mysql — OrionBelt Semantic Layer Driver for MySQL

## Purpose

PEP 249 DB-API 2.0 driver wrapping `mysql-connector-python >= 8.0` that intercepts OBML YAML
queries, compiles them to MySQL SQL via the OrionBelt REST API (standalone mode), and executes
them natively.

Unlike the postgres driver (which uses ADBC for native Arrow), this driver fetches rows via
`mysql-connector-python` tuples and converts to PyArrow Tables on demand in `fetch_arrow_table()`.

**OB dialect string:** `"mysql"`
**Author:** Ralfo Becher / RALFORION d.o.o. (info@orionbelt.ai)
**License:** Apache 2.0

---

## Module Map

| File | Responsibility |
|---|---|
| `__init__.py` | PEP 249 connect() + module constants |
| `connection.py` | Connection class |
| `cursor.py` | Cursor class — OBML detection + execution + Arrow conversion |
| `compiler.py` | Re-export from ob-driver-core (OBML detection + REST compilation) |
| `exceptions.py` | PEP 249 exception hierarchy (re-export from ob-driver-core) |
| `type_codes.py` | PEP 249 type objects + MySQL field type mapping |

---

## connect() Parameters

### MySQL-specific
| Parameter | Type | Default | Description |
|---|---|---|---|
| host | str | localhost | MySQL host |
| port | int | 3306 | MySQL port |
| database | str | — | Database name |
| user | str | None | Username |
| password | str | None | Password |
| ssl_ca | str | None | Path to CA cert |
| ssl_cert | str | None | Path to client cert |
| ssl_key | str | None | Path to client key |
| charset | str | utf8mb4 | Character set |

### OrionBelt parameters (same across all vendors)
| Parameter | Default | Description |
|---|---|---|
| ob_api_url | http://localhost:8000 | OB REST API URL (must be running in single-model mode) |
| ob_timeout | 30 | HTTP timeout in seconds |

---

## Vendor-Specific Notes

- `mysql-connector-python` uses `%s` (format) paramstyle
- No official Apache ADBC MySQL driver exists. A third-party option
  ([Columnar `adbc-drivers/mysql`](https://github.com/columnar-com/adbc-drivers), Apache-2.0,
  Oct 2025) is available but installed via `dbc install mysql`, not a standalone PyPI package.
  Current approach uses `mysql-connector-python` with emulated `fetch_arrow_table()` for stability.
  Re-evaluate if a first-party ADBC driver becomes available or Columnar publishes to PyPI.
- `commit()` and `rollback()` are meaningful — MySQL uses real transactions
- Default charset is `utf8mb4` for full Unicode support
- MySQL 8.0+ required (CTE support)
- `group_concat_max_len` may need increasing for large LISTAGG results

---

## Type System

`mysql-connector-python` cursor.description returns field type constants as integers.
`MYSQL_TYPE_MAP` maps common MySQL field types to PEP 249 type objects:
- 0=DECIMAL, 1=TINY, 3=LONG, 4=FLOAT, 5=DOUBLE, 8=LONGLONG → NUMBER
- 7=TIMESTAMP, 10=DATE, 11=TIME, 12=DATETIME → DATETIME
- 15=VARCHAR, 253=VAR_STRING, 254=STRING → STRING
- 249-252=BLOB variants → BINARY

---

## Dependencies

```toml
[project.dependencies]
mysql-connector-python = ">=8.0"
pyarrow = ">=16.0"
ob-driver-core = ">=0.1"
```
