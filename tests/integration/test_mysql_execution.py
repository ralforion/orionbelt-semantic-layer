"""Integration tests: compile + execute the commerce battery on real MySQL.

The full ``COMMERCE_CASES`` battery defined in
``tests/integration/_commerce.py`` runs against a MySQL container. DuckDB
executes the same queries against the same parquet fixtures and acts as
the source of truth — any row-level disagreement is a MySQL dialect bug.

Opt-in — requires Docker::

    uv run pytest -m docker

Skipped automatically when:
- testcontainers / pymysql / pandas / pyarrow are not installed
- the Docker daemon is not reachable
"""

from __future__ import annotations

import pytest

testcontainers_mysql = pytest.importorskip(
    "testcontainers.mysql", reason="testcontainers[mysql] required"
)
pymysql = pytest.importorskip("pymysql", reason="pymysql required")
pd = pytest.importorskip("pandas", reason="pandas required for bulk-load")
pytest.importorskip("pyarrow", reason="pyarrow required to read parquet")

from testcontainers.mysql import MySqlContainer  # noqa: E402

from tests.integration._commerce import (  # noqa: E402
    COMMERCE_CASES,
    COMMERCE_TABLES,
    CommerceCase,
    compare_rows,
    compile_for,
    fetch_duckdb,
    load_commerce_model,
    open_duckdb_truth,
    parquet_path,
)

pytestmark = pytest.mark.docker


# MySQL's "schema" is a database. We use a single database whose name matches
# the OBML model's ``schema:`` field so the compiled SQL (``orionbelt_1.sales``)
# resolves cleanly.
_SCHEMA = "orionbelt_1"


_MYSQL_TYPE_MAP = {
    "int64": "BIGINT",
    "int32": "INT",
    "float64": "DOUBLE",
    "float32": "FLOAT",
    "bool": "TINYINT(1)",
}


def _docker_available() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return True
    except Exception:  # noqa: BLE001
        return False


def _mysql_type_for(dtype) -> str:
    s = str(dtype)
    if s.startswith("datetime64"):
        return "DATETIME"
    if s == "object":
        return "VARCHAR(255)"
    return _MYSQL_TYPE_MAP.get(s, "VARCHAR(255)")


def _load_parquet(cur, schema: str, table: str) -> None:
    """CREATE TABLE + INSERT one parquet fixture via executemany."""
    df = pd.read_parquet(parquet_path(table))
    cols_ddl = ", ".join(f"`{c}` {_mysql_type_for(df[c].dtype)}" for c in df.columns)
    cur.execute(f"CREATE TABLE `{schema}`.`{table}` ({cols_ddl})")
    if df.empty:
        return
    quoted_cols = ", ".join(f"`{c}`" for c in df.columns)
    placeholders = ", ".join(["%s"] * len(df.columns))
    rows = [tuple(None if pd.isna(v) else v for v in row) for row in df.itertuples(index=False)]
    cur.executemany(
        f"INSERT INTO `{schema}`.`{table}` ({quoted_cols}) VALUES ({placeholders})",
        rows,
    )


@pytest.fixture(scope="module")
def mysql_setup():
    """Spin up MySQL, load all parquet tables into the container's default DB.

    The testcontainers MySQL image gives the non-root ``test`` user permission
    only on the bundled ``test`` database — creating a fresh database for the
    commerce schema would 1044 with "Access denied". We instead load the
    commerce tables into the default database and rewrite the model's schema
    to match.
    """
    if not _docker_available():
        pytest.skip("Docker is not running")

    with MySqlContainer("mysql:8.0") as my:
        conn = pymysql.connect(
            host=my.get_container_host_ip(),
            port=int(my.get_exposed_port(3306)),
            user=my.username,
            password=my.password,
            database=my.dbname,
            autocommit=True,
        )
        cur = conn.cursor()
        schema = my.dbname
        for table in COMMERCE_TABLES:
            _load_parquet(cur, schema, table)
        cur.close()
        yield conn, schema
        conn.close()


@pytest.fixture(scope="module")
def vendor_model(mysql_setup):
    _conn, schema = mysql_setup
    return load_commerce_model(database="mysql", schema=schema)


@pytest.fixture(scope="module")
def truth_model(mysql_setup):
    _conn, schema = mysql_setup
    return load_commerce_model(database="main", schema=schema)


@pytest.fixture(scope="module")
def truth_results(truth_model, mysql_setup):
    _conn, schema = mysql_setup
    con = open_duckdb_truth(schema=schema)
    try:
        return {
            case.name: fetch_duckdb(con, compile_for(case.query, truth_model, "duckdb"))
            for case in COMMERCE_CASES
        }
    finally:
        con.close()


def _fetch_mysql(conn, sql: str) -> list[dict]:
    cur = conn.cursor(pymysql.cursors.DictCursor)
    try:
        cur.execute(sql)
        return list(cur.fetchall())
    finally:
        cur.close()


# MySQL has no GROUP BY CUBE — the dialect raises NotImplementedError on
# compile. Mark just that case as expected-skip so the rest of the battery
# still gates on real dialect bugs.
_MYSQL_UNSUPPORTED = {"cube_sales_by_country_category"}


def _parametrize_cases():
    out = []
    for case in COMMERCE_CASES:
        if case.name in _MYSQL_UNSUPPORTED:
            out.append(
                pytest.param(case, marks=pytest.mark.skip(reason="MySQL has no GROUP BY CUBE"))
            )
        else:
            out.append(case)
    return out


@pytest.mark.parametrize("case", _parametrize_cases(), ids=lambda c: c.name)
def test_commerce_case(mysql_setup, vendor_model, truth_results, case: CommerceCase) -> None:
    conn, _schema = mysql_setup
    sql = compile_for(case.query, vendor_model, "mysql")
    actual = _fetch_mysql(conn, sql)
    compare_rows(actual, truth_results[case.name], case=case.name)
