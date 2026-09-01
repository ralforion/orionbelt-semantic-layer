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


def test_a_period_over_period_month_comes_back_as_a_date(mysql_setup, vendor_model) -> None:
    """The PoP time dimension is a date here too, not a formatted string.

    The spine's bucket goes through the string-level truncation rather than the
    dimension grain, and MySQL's recursive CTE takes ``spine_date``'s type from
    its anchor row: a ``DATE_FORMAT`` bucket made it ``varchar(10)``, so the
    same ``Sales Month`` came back as a date without a PoP metric in the query
    and as text with one. Executed, because the string sorts and reads the same
    and only the type gives it away.
    """
    import datetime

    from orionbelt.models.query import QueryObject, QuerySelect

    conn, _schema = mysql_setup
    query = QueryObject(
        select=QuerySelect(dimensions=["Sales Month"], measures=["Sales YoY Growth"])
    )
    rows = _fetch_mysql(conn, compile_for(query, vendor_model, "mysql"))
    assert rows, "the PoP query returned no rows"
    month = rows[0]["Sales Month"]
    assert isinstance(month, datetime.date), f"the month came back as {type(month).__name__}"


_HOURLY_MODEL_YAML = """\
version: 1.0
dataObjects:
  Events:
    code: pop_events
    schema: {schema}
    columns:
      Event ID: {{code: id, abstractType: string}}
      Occurred At: {{code: occurred_at, abstractType: timestamp}}
      Amount: {{code: amount, abstractType: float, numClass: additive}}
dimensions:
  Occurred Hour:
    dataObject: Events
    column: Occurred At
    resultType: timestamp
    timeGrain: hour
measures:
  Revenue:
    columns: [{{dataObject: Events, column: Amount}}]
    resultType: float
    aggregation: sum
metrics:
  Revenue HoH Diff:
    type: period_over_period
    expression: '{{[Revenue]}}'
    periodOverPeriod:
      timeDimension: Occurred Hour
      grain: hour
      offset: -1
      offsetGrain: hour
      comparison: difference
"""


@pytest.fixture(scope="module")
def hourly_events(mysql_setup):
    """Four rows across three hours of one day, and the model that reads them."""
    from orionbelt.parser.loader import TrackedLoader
    from orionbelt.parser.resolver import ReferenceResolver

    conn, schema = mysql_setup
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS pop_events")
    cur.execute(
        "CREATE TABLE pop_events (id VARCHAR(8), occurred_at DATETIME, amount DECIMAL(10, 2))"
    )
    cur.execute(
        "INSERT INTO pop_events VALUES ('a', '2024-03-15 09:15:00', 10.0),"
        " ('b', '2024-03-15 09:40:00', 5.0), ('c', '2024-03-15 10:20:00', 20.0),"
        " ('d', '2024-03-15 11:05:00', 7.0)"
    )
    cur.close()
    raw, source_map = TrackedLoader().load_string(_HOURLY_MODEL_YAML.format(schema=schema))
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def test_an_hourly_period_over_period_compares_hours(mysql_setup, hourly_events) -> None:
    """``grain: hour`` buckets by the hour, rather than summing the whole day.

    ``PeriodOverPeriod.grain`` is the whole ``TimeGrain`` enum, and the spine's
    bucket comes from the string-level truncation, which had no entry for the
    sub-day grains and fell through to ``DATE(...)``. The three hours collapsed
    into one row of 42.00 under a date, with nothing to compare it to, and no
    error anywhere: the wrong answer here is a plausible-looking one.
    """
    import datetime

    from orionbelt.models.query import QueryObject, QuerySelect

    conn, _schema = mysql_setup
    query = QueryObject(
        select=QuerySelect(dimensions=["Occurred Hour"], measures=["Revenue", "Revenue HoH Diff"])
    )
    rows = _fetch_mysql(conn, compile_for(query, hourly_events, "mysql"))
    assert [(r["Occurred Hour"], float(r["Revenue"]), r["Revenue HoH Diff"]) for r in rows] == [
        (datetime.datetime(2024, 3, 15, 9, 0), 15.0, None),
        (datetime.datetime(2024, 3, 15, 10, 0), 20.0, 5.0),
        (datetime.datetime(2024, 3, 15, 11, 0), 7.0, -13.0),
    ]


@pytest.mark.parametrize(
    "grain", ["year", "quarter", "month", "week", "day", "hour", "minute", "second"]
)
def test_both_truncation_paths_answer_the_same_value(mysql_setup, hourly_events, grain) -> None:
    """The dimension grain and the spine's bucket are the same truncation.

    They are rendered by two helpers -- one over the AST for a dimension, one
    over SQL text for the period-over-period spine -- and they have drifted
    twice: once on the cast that DATE_FORMAT's text needs, once on the sub-day
    grains. Compared by value rather than by spelling, so a difference in how
    either is written cannot hide one in what it answers.
    """
    from orionbelt.ast.nodes import RawSQL
    from orionbelt.dialect.registry import DialectRegistry
    from orionbelt.models.semantic import TimeGrain

    conn, _schema = mysql_setup
    dialect = DialectRegistry.get("mysql")
    column = "`occurred_at`"
    grained = dialect.compile_expr(dialect.render_time_grain(RawSQL(sql=column), TimeGrain(grain)))
    bucketed = dialect.render_date_trunc_sql(column, grain)
    rows = _fetch_mysql(
        conn, f"SELECT {grained} AS grained, {bucketed} AS bucketed FROM pop_events ORDER BY id"
    )
    for row in rows:
        assert row["grained"] == row["bucketed"], f"{grain}: {grained} vs {bucketed}"
