"""Integration tests: ob-mysql PEP 249 driver against a real MySQL via testcontainers.

Tests the full driver path: OBML YAML → detect → compile → execute → fetch.
The REST API compilation is replaced with direct Python compilation to avoid
needing a running API server.

    uv run pytest -m docker

Skipped automatically when:
- ob-mysql, testcontainers, or pymysql packages are not installed
- Docker is not running

Every test runs through both of ob-mysql's clients: the ADBC MySQL driver
(skipped where it is not installed: ``pip install dbc && dbc install mysql``)
and PyMySQL, its fallback.
"""

from __future__ import annotations

import datetime
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

import pytest

# Skip entire module if dependencies are missing
pytest.importorskip("testcontainers.mysql", reason="testcontainers[mysql] required")
ob_mysql = pytest.importorskip("ob_mysql", reason="ob-mysql driver required")
pytest.importorskip("pyarrow", reason="pyarrow required")

import pyarrow as pa  # noqa: E402
from testcontainers.mysql import MySqlContainer  # noqa: E402

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.models.query import QueryObject  # noqa: E402
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402
from tests.conftest import SALES_MODEL_DIR  # noqa: E402

pytestmark = pytest.mark.docker

# ---------------------------------------------------------------------------
# Test data — same as test_mysql_execution.py
# ---------------------------------------------------------------------------

_SETUP_SQL = """\
CREATE DATABASE IF NOT EXISTS `PUBLIC`;

CREATE TABLE `PUBLIC`.`CUSTOMERS` (
    `CUSTOMER_ID` VARCHAR(255), `NAME` VARCHAR(255),
    `COUNTRY` VARCHAR(255), `SEGMENT` VARCHAR(255)
);
INSERT INTO `PUBLIC`.`CUSTOMERS` VALUES
    ('C1', 'Alice',   'US', 'SMB'),
    ('C2', 'Bob',     'UK', 'Enterprise'),
    ('C3', 'Charlie', 'US', 'MidMarket');

CREATE TABLE `PUBLIC`.`PRODUCTS` (
    `PRODUCT_ID` VARCHAR(255), `NAME` VARCHAR(255), `CATEGORY` VARCHAR(255)
);
INSERT INTO `PUBLIC`.`PRODUCTS` VALUES
    ('P1', 'Widget', 'Hardware'),
    ('P2', 'Gadget', 'Software');

CREATE TABLE `PUBLIC`.`ORDERS` (
    `ORDER_ID` VARCHAR(255), `ORDER_DATE` DATE, `CUSTOMER_ID` VARCHAR(255),
    `PRODUCT_ID` VARCHAR(255), `QUANTITY` INT, `PRICE` DOUBLE
);
INSERT INTO `PUBLIC`.`ORDERS` VALUES
    ('O1', '2024-01-15', 'C1', 'P1', 10,  5.0),
    ('O2', '2024-01-20', 'C1', 'P2',  2, 25.0),
    ('O3', '2024-02-10', 'C2', 'P1',  5,  5.0),
    ('O4', '2024-02-15', 'C3', 'P2',  1, 100.0),
    ('O5', '2024-03-01', 'C2', 'P1',  3,  5.0);
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def sales_model() -> SemanticModel:
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, source_map = loader.load(SALES_MODEL_DIR / "model.yaml")
    model, result = resolver.resolve(raw, source_map)
    assert result.valid
    return model


@pytest.fixture(scope="module")
def _patch_compiler(sales_model: SemanticModel) -> Any:
    """Replace REST-based compile_obml with direct Python compilation."""
    import ob_mysql.cursor as cursor_mod

    pipeline = CompilationPipeline()
    original = cursor_mod.compile_obml

    def direct_compile(
        obml: dict[str, Any],
        *,
        dialect: str,
        ob_api_url: str = "",
        ob_timeout: int = 30,
    ) -> str:
        query = QueryObject.model_validate(obml)
        return pipeline.compile(query, sales_model, dialect).sql

    cursor_mod.compile_obml = direct_compile  # type: ignore[assignment]
    yield
    cursor_mod.compile_obml = original  # type: ignore[assignment]


def _adbc_installed() -> bool:
    try:
        import adbc_driver_manager
        import adbc_driver_manager.dbapi
    except ImportError:
        return False
    try:
        adbc_driver_manager.dbapi.connect(
            driver=ob_mysql.ADBC_DRIVER, db_kwargs={"uri": "mysql://x@127.0.0.1:1/x"}
        )
    except adbc_driver_manager.Error as error:
        return error.status_code != adbc_driver_manager.AdbcStatusCode.NOT_FOUND
    return True


@pytest.fixture(scope="module")
def mysql_server(_patch_compiler: Any):
    """Spin up MySQL, seed data, return the connection arguments."""
    if not _docker_available():
        pytest.skip("Docker is not running")

    import pymysql

    with MySqlContainer("mysql:8.0") as mysql_c:
        server = {
            "host": mysql_c.get_container_host_ip(),
            "port": int(mysql_c.get_exposed_port(3306)),
            "user": "root",
            "password": mysql_c.root_password,
        }
        raw = pymysql.connect(**server, autocommit=True)
        with raw.cursor() as cur:
            for stmt in _SETUP_SQL.split(";"):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
        raw.close()
        yield {**server, "database": "PUBLIC"}


# Ids that are not ``adbc``: the conftest skips a test carrying that keyword
# unless ``-m adbc`` (the Postgres ADBC suite) is selected.
@pytest.fixture(
    scope="module", params=["adbc", "pymysql"], ids=["adbc_mysql_driver", "pymysql_client"]
)
def mysql_conn(request: pytest.FixtureRequest, mysql_server: dict[str, Any]):
    """An ob-mysql connection through each client in turn."""
    if request.param == "adbc" and not _adbc_installed():
        pytest.skip("the ADBC MySQL driver is not installed (dbc install mysql)")
    conn = ob_mysql.connect(**mysql_server, backend=request.param)
    assert conn.backend == request.param
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# OBML query execution via driver
# ---------------------------------------------------------------------------

_REVENUE_BY_COUNTRY = """\
select:
  dimensions:
    - Customer Country
  measures:
    - Revenue
"""

_MULTI_MEASURE = """\
select:
  dimensions:
    - Customer Country
  measures:
    - Revenue
    - Order Count
"""

_REVENUE_SHARE = """\
select:
  dimensions:
    - Customer Country
  measures:
    - Revenue Share
"""


def _rows_to_dicts(cursor: Any) -> list[dict[str, Any]]:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row, strict=False)) for row in cursor.fetchall()]


class TestOBMySQLDriver:
    """Execute OBML YAML queries through the ob-mysql PEP 249 driver."""

    def test_obml_revenue_by_country(self, mysql_conn) -> None:
        cur = mysql_conn.cursor()
        cur.execute(_REVENUE_BY_COUNTRY)
        rows = _rows_to_dicts(cur)
        cur.close()

        by_country = {r["Customer Country"]: r["Revenue"] for r in rows}
        assert float(by_country["US"]) == pytest.approx(200.0)
        assert float(by_country["UK"]) == pytest.approx(40.0)

    def test_obml_multi_measure(self, mysql_conn) -> None:
        cur = mysql_conn.cursor()
        cur.execute(_MULTI_MEASURE)
        rows = _rows_to_dicts(cur)
        cur.close()

        by_country = {r["Customer Country"]: r for r in rows}
        assert float(by_country["US"]["Revenue"]) == pytest.approx(200.0)
        assert by_country["US"]["Order Count"] == 3

    def test_obml_derived_metric(self, mysql_conn) -> None:
        cur = mysql_conn.cursor()
        cur.execute(_REVENUE_SHARE)
        rows = _rows_to_dicts(cur)
        cur.close()

        by_country = {r["Customer Country"]: r["Revenue Share"] for r in rows}
        assert float(by_country["US"]) == pytest.approx(200.0 / 240.0, rel=1e-3)
        assert float(by_country["UK"]) == pytest.approx(40.0 / 240.0, rel=1e-3)

    def test_plain_sql_passthrough(self, mysql_conn) -> None:
        """Plain SQL bypasses OBML compilation."""
        cur = mysql_conn.cursor()
        cur.execute("SELECT 1 AS n")
        rows = cur.fetchall()
        cur.close()
        assert rows == [(1,)]

    def test_fetch_arrow_table(self, mysql_conn) -> None:
        cur = mysql_conn.cursor()
        cur.execute(_REVENUE_BY_COUNTRY)
        table = cur.fetch_arrow_table()
        cur.close()

        assert isinstance(table, pa.Table)
        assert table.num_rows == 2
        assert "Customer Country" in table.column_names
        assert "Revenue" in table.column_names

    def test_cursor_description(self, mysql_conn) -> None:
        cur = mysql_conn.cursor()
        cur.execute(_REVENUE_BY_COUNTRY)
        assert cur.description is not None
        col_names = [d[0] for d in cur.description]
        assert "Customer Country" in col_names
        assert "Revenue" in col_names
        cur.fetchall()  # consume results before close (MySQL requires this)
        cur.close()


# ---------------------------------------------------------------------------
# Arrow schema stability across every MySQL type
# ---------------------------------------------------------------------------

_ALL_TYPES_DDL = """CREATE TABLE alltypes (
  c_dec DECIMAL(18,2), c_tiny TINYINT, c_bool TINYINT(1), c_small SMALLINT,
  c_medium MEDIUMINT, c_int INT, c_big BIGINT, c_ubig BIGINT UNSIGNED,
  c_float FLOAT, c_double DOUBLE, c_bit BIT(8), c_date DATE, c_time TIME,
  c_dt DATETIME, c_ts TIMESTAMP NULL, c_year YEAR, c_char CHAR(4),
  c_vchar VARCHAR(20), c_text TEXT, c_blob BLOB, c_enum ENUM('a','b'),
  c_set SET('a','b'), c_json JSON, c_bin BINARY(4))"""

_ALL_TYPES_ROW = """INSERT INTO alltypes VALUES (2.55, 1, 1, 2, 3, 4, 5,
  18446744073709551615, 1.5, 2.5, b'10101010', '2026-08-15', '01:02:03',
  '2026-08-15 13:45:00', '2026-08-15 13:45:00', 2026, 'ab', 'x', 'y', 'z',
  'a', 'a,b', '{"k": 1}', 'bin')"""


@pytest.fixture(scope="module")
def alltypes_conn(mysql_conn):
    """The same connection, with a table covering every MySQL column type."""
    cur = mysql_conn.cursor()
    cur.execute("DROP TABLE IF EXISTS alltypes")
    cur.execute(_ALL_TYPES_DDL)
    cur.execute(_ALL_TYPES_ROW)
    cur.close()
    return mysql_conn


def _schema_of(conn, sql: str) -> pa.Schema:
    cur = conn.cursor()
    cur.execute(sql)
    schema = cur.fetch_arrow_table().schema
    cur.close()
    return schema


def _table(conn, sql: str) -> pa.Table:
    cur = conn.cursor()
    cur.execute(sql)
    table = cur.fetch_arrow_table()
    cur.close()
    return table


#: Each column's type, through both clients: what ADBC and PyMySQL read
#: differently is brought to one schema (see ``ob_mysql.arrow_types``).
_ALL_TYPES_SCHEMA = {
    "c_dec": pa.decimal128(18, 2),
    "c_tiny": pa.int8(),
    "c_bool": pa.int8(),
    "c_small": pa.int16(),
    "c_medium": pa.int32(),
    "c_int": pa.int32(),
    "c_big": pa.int64(),
    "c_ubig": pa.uint64(),
    "c_float": pa.float32(),
    "c_double": pa.float64(),
    "c_bit": pa.uint64(),
    "c_date": pa.date32(),
    "c_time": pa.duration("us"),
    "c_dt": pa.timestamp("us"),
    "c_ts": pa.timestamp("us"),
    "c_year": pa.int16(),
    "c_char": pa.string(),
    "c_vchar": pa.string(),
    "c_text": pa.string(),
    "c_blob": pa.binary(),
    "c_enum": pa.string(),
    "c_set": pa.string(),
    "c_json": pa.string(),
    "c_bin": pa.binary(),
}


class TestArrowSchemaComesFromMetadata:
    """A column's Arrow type must not change with the rows, or with the client."""

    def test_every_type_maps_the_same_through_either_client(self, alltypes_conn) -> None:
        schema = _schema_of(alltypes_conn, "SELECT * FROM alltypes")
        assert {f.name: f.type for f in schema} == _ALL_TYPES_SCHEMA

    def test_every_type_survives_an_empty_result_unchanged(self, alltypes_conn) -> None:
        """The decimal included: its scale used to be read off the values."""
        populated = _schema_of(alltypes_conn, "SELECT * FROM alltypes")
        empty = _schema_of(alltypes_conn, "SELECT * FROM alltypes WHERE 1=0")
        assert populated == empty

    def test_all_null_column_keeps_the_type_of_a_populated_one(self, alltypes_conn) -> None:
        cur = alltypes_conn.cursor()
        cur.execute("INSERT INTO alltypes (c_int) VALUES (NULL)")
        cur.close()
        nulls = _schema_of(alltypes_conn, "SELECT c_bit, c_set, c_blob FROM alltypes WHERE 1=0")
        populated = _schema_of(alltypes_conn, "SELECT c_bit, c_set, c_blob FROM alltypes")
        assert nulls == populated

    def test_a_wide_decimal_reports_one_width_for_every_filter(self, alltypes_conn) -> None:
        """Two filters over one DECIMAL(65, 2) column must agree on the type."""
        cur = alltypes_conn.cursor()
        cur.execute("DROP TABLE IF EXISTS wide_decimal")
        cur.execute("CREATE TABLE wide_decimal (d DECIMAL(65,2))")
        cur.execute("INSERT INTO wide_decimal VALUES (1.50), (%s)" % ("9" * 40 + ".99"))
        cur.close()

        small = _schema_of(alltypes_conn, "SELECT d FROM wide_decimal WHERE d < 2")
        large = _schema_of(alltypes_conn, "SELECT d FROM wide_decimal WHERE d > 2")
        none = _schema_of(alltypes_conn, "SELECT d FROM wide_decimal WHERE d < 0")
        assert small == large == none
        assert small.field("d").type == pa.decimal256(65, 2)
        table = _table(alltypes_conn, "SELECT d FROM wide_decimal WHERE d > 2")
        assert table.column(0)[0].as_py() == Decimal("9" * 40 + ".99")

    def test_values_survive_the_declared_types(self, alltypes_conn) -> None:
        table = _table(
            alltypes_conn,
            "SELECT c_bit, c_set, c_ubig, c_dec, c_year, c_time, c_json FROM alltypes "
            "WHERE c_int = 4",
        )
        assert table.to_pylist() == [
            {
                "c_bit": 170,
                "c_set": "a,b",
                "c_ubig": 18446744073709551615,
                "c_dec": Decimal("2.55"),
                "c_year": 2026,
                "c_time": datetime.timedelta(hours=1, minutes=2, seconds=3),
                "c_json": '{"k": 1}',
            }
        ]


class TestWhatTheADBCDriverReadsDifferently:
    """Each case was a failed result or a moved value through the ADBC driver
    alone, and reads as PyMySQL reads it now."""

    def test_a_time_outside_a_day(self, mysql_conn) -> None:
        """ADBC reads TIME as a clock reading and refuses -838:59:59 or 25:00."""
        table = _table(
            mysql_conn,
            "SELECT CAST('-838:59:59' AS TIME) AS low, CAST('25:00:00' AS TIME) AS high",
        )
        assert table.schema.types == [pa.duration("us"), pa.duration("us")]
        assert table.to_pylist() == [
            {
                "low": -datetime.timedelta(hours=838, minutes=59, seconds=59),
                "high": datetime.timedelta(hours=25),
            }
        ]

    def test_a_zero_date_reads_as_null(self, mysql_conn) -> None:
        cur = mysql_conn.cursor()
        cur.execute("SET SESSION sql_mode = ''")
        cur.execute("SELECT CAST('0000-00-00 00:00:00' AS DATETIME) AS z, DATE('2024-01-01') AS d")
        assert cur.fetchall() == [(None, datetime.date(2024, 1, 1))]
        cur.execute("SET SESSION sql_mode = DEFAULT")
        cur.close()

    def test_the_session_keeps_the_servers_time_zone(self, mysql_conn) -> None:
        """The ADBC driver sets the session to UTC, moving NOW(), CURRENT_DATE
        and every TIMESTAMP away from the server's zone."""
        table = _table(mysql_conn, "SELECT @@session.time_zone = @@global.time_zone AS same")
        assert table.column(0).to_pylist() == [1]

    def test_parameters_are_escaped(self, mysql_conn) -> None:
        cur = mysql_conn.cursor()
        cur.execute("SELECT %s AS a, %s AS b, %s AS c", ("it's", None, 3))
        assert cur.fetchall() == [("it's", None, 3)]
        cur.close()


def test_concurrent_fetches_stay_concurrent(mysql_conn, mysql_server) -> None:
    """Ten threads, each with its own connection, fetching 100k rows at once.

    mysql-connector-python released the GIL once or more per row, so ten such
    fetches took 21 s where one took 0.08 s (#619). The bound here is loose on
    purpose: it fails on that, not on a slow machine.
    """
    if mysql_conn.backend != "adbc":
        pytest.skip("PyMySQL is bound by the GIL; its scaling depends on the Python build")
    sql = (
        "WITH RECURSIVE n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 100000) "
        "SELECT i, i * 2 FROM n"
    )

    def fetch(_: int) -> int:
        conn = ob_mysql.connect(**mysql_server, backend=mysql_conn.backend)
        cur = conn.cursor()
        cur.execute("SET SESSION cte_max_recursion_depth = 100001")
        cur.execute(sql)
        rows = cur.fetch_arrow_table().num_rows
        conn.close()
        return rows

    start = time.perf_counter()
    assert fetch(0) == 100_000
    alone = time.perf_counter() - start
    with ThreadPoolExecutor(10) as pool:
        start = time.perf_counter()
        assert list(pool.map(fetch, range(10))) == [100_000] * 10
        together = time.perf_counter() - start
    assert together < 10 * alone + 2, f"one fetch {alone:.2f} s, ten at once {together:.2f} s"
