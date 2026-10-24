# ob-mysql

OrionBelt Semantic Layer driver for MySQL 8.0+ (DB-API 2.0)

Part of the [OrionBelt Semantic Layer](https://github.com/ralforion/orionbelt-semantic-layer) project.

## Installation

```bash
pip install ob-mysql
```

`ob-mysql` reads MySQL through the [ADBC MySQL driver](https://github.com/adbc-drivers/mysql)
(Arrow-native) when it is installed, and through PyMySQL otherwise. The ADBC
driver is not on PyPI; install it with [`dbc`](https://columnar.tech/dbc):

```bash
pip install dbc
dbc install mysql
```

The OrionBelt Docker images that ship the vendor drivers carry it already.
Both clients return the same Arrow types. The ADBC driver keeps fetches on
several threads concurrent, where a pure-Python client is bound by the GIL.

`connect(backend=...)` picks one explicitly: `"auto"` (default), `"adbc"` or
`"pymysql"`. A connection with `ssl_ca`, `ssl_cert` or `ssl_key` uses PyMySQL,
since the ADBC driver takes no certificate files.

## License

BUSL-1.1 — see [LICENSE](LICENSE) for details.
