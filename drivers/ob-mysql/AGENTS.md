# ob-mysql — OrionBelt Semantic Layer Driver for MySQL

## Purpose

PEP 249 DB-API 2.0 driver that intercepts OBML YAML queries, compiles them to MySQL SQL via the
OrionBelt REST API (standalone mode), and executes them natively.

MySQL is read through the ADBC Driver Foundry MySQL driver (native Arrow, loaded by
`adbc-driver-manager` under the name `mysql`) when it is installed, and through PyMySQL
otherwise. `connection.backend` says which. Both return one Arrow schema for a result
(`arrow_types.py`).

mysql-connector-python was replaced (#619): it releases the GIL once or more per row, and ten
threads fetching 100k rows each took 21 s where one took 0.08 s.

**OB dialect string:** `"mysql"`
**Author:** Ralf Becher / RALFORION d.o.o. (info@orionbelt.ai)
**License:** Apache 2.0

---

## Module Map

| File | Responsibility |
|---|---|
| `__init__.py` | PEP 249 connect() + module constants |
| `connection.py` | Connection class |
| `cursor.py` | Cursor class — OBML detection, execution, paging through the fetched result |
| `arrow_types.py` | One Arrow schema for both clients: PyMySQL rows built from column specs, ADBC tables normalized |
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
| backend | str | auto | `auto` (ADBC if installed, else PyMySQL), `adbc`, `pymysql`; any `ssl_*` path means PyMySQL |

### OrionBelt parameters (same across all vendors)
| Parameter | Default | Description |
|---|---|---|
| ob_api_url | http://localhost:8000 | OB REST API URL (must be running in single-model mode) |
| ob_timeout | 30 | HTTP timeout in seconds |

---

## Vendor-Specific Notes

- `format` paramstyle (`%s`) on both clients: parameters are escaped into the statement on the
  client (PyMySQL's own escaping), and ADBC is given the finished statement.
- The ADBC driver is not on PyPI: `pip install dbc && dbc install mysql`. The Docker images
  install a pinned, checksummed release with `scripts/install_adbc_mysql.py`
  (`ADBC_DRIVER_PATH=/opt/adbc/drivers`).
- What the ADBC driver does differently, measured against MySQL 8.0, and what the driver does
  about it:
  - It sets the session time zone to UTC: the connection sets it back to
    `@@global.time_zone`, so `NOW()`, `CURRENT_DATE` and TIMESTAMP values read as with PyMySQL.
  - No TLS by default: the URI asks for `tls=preferred`. It takes no certificate files, so a
    connection with `ssl_*` paths uses PyMySQL.
  - A zero date fails the result: the statement option
    `mysql.query.zero_datetime_behavior=convert_to_null` reads it as NULL (a connection option
    is ignored).
  - A TIME outside 00:00:00-23:59:59 fails the result ("failed to append value"): a reading
    statement is run again through a PyMySQL connection to the same server.
  - It does not report `rowcount` for DML or `lastrowid`.
- `commit()` and `rollback()` are meaningful — MySQL uses real transactions
- Default charset is `utf8mb4` for full Unicode support
- MySQL 8.0+ required (CTE support)
- `group_concat_max_len` may need increasing for large LISTAGG results

---

## Type System

Both clients report the same Arrow types (`arrow_types.py`): DECIMAL at its declared precision
(decimal128, or decimal256 past 38 digits), DATETIME and TIMESTAMP as `timestamp[us]` (the
wall clock MySQL shows), TIME as `duration[us]`, YEAR as int16, BIT as uint64, JSON as string.
`MYSQL_TYPE_MAP` maps MySQL field types to PEP 249 type objects for `cursor.description`.

---

## Dependencies

```toml
[project.dependencies]
adbc-driver-manager = ">=1.8"
pymysql = ">=1.1"
pyarrow = ">=16.0"
ob-driver-core = ">=0.1"
```
