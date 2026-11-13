"""A DuckDB data source is opened alike on every surface (``duckdb_source``).

Setup statements run on every connection; a source without a database file
keeps one instance, so its queries share a memory limit; memory, thread and
spill settings reach DuckDB.
"""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("ob_flight")
duckdb = pytest.importorskip("duckdb")

from ob_flight import db_router  # noqa: E402
from ob_flight.datasources import DataSource  # noqa: E402

from orionbelt.cli import _local  # noqa: E402
from orionbelt.models.query import QueryObject  # noqa: E402
from orionbelt.service import duckdb_source  # noqa: E402
from orionbelt.service import federated_executor as federation  # noqa: E402
from orionbelt.service.datasource_selection import describe_datasources  # noqa: E402
from orionbelt.service.db_executor import (  # noqa: E402
    ExecutionError,
    ExecutionUnavailableError,
    execute_sql,
)


def lake(**variables: str) -> DataSource:
    """A named DuckDB source ``lake`` with these ``DB_LAKE_`` variables."""
    environ = {f"DB_LAKE_{name}": value for name, value in variables.items()}
    return DataSource(name="lake", dialect="duckdb", prefix="DB_LAKE_", environ=environ)


def value(source: DataSource, sql: str) -> Any:
    return execute_sql(sql, dialect="duckdb", source=source).rows[0][0]


@pytest.fixture(autouse=True)
def _close_instances() -> Iterator[None]:
    yield
    duckdb_source.close_instances()


@pytest.fixture
def on_disk(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The temp directory is on a disk, not in memory."""
    for variable in ("K_SERVICE", "CLOUD_RUN_JOB"):
        monkeypatch.delenv(variable, raising=False)
    with patch.object(federation, "_filesystem_type", return_value="ext4"):
        yield


@pytest.fixture
def orders(tmp_path: Any) -> str:
    """Two Parquet files of orders, ``parts/*.parquet``: ids 0-9, amount 2 * id."""
    con = duckdb.connect()
    (tmp_path / "parts").mkdir()
    for part in range(2):
        con.execute(
            "COPY (SELECT i AS id, i * 2.0 AS amount, 'c' || (i % 2) AS customer "
            f"FROM range({part * 5}, {part * 5 + 5}) t(i)) "
            f"TO '{tmp_path / 'parts' / f'p{part}.parquet'}'"
        )
    con.close()
    return str(tmp_path / "parts" / "*.parquet")


def parts(orders: str) -> str:
    """The directory of the ``orders`` files, for ``DUCKDB_ALLOWED_PATHS``."""
    return os.path.dirname(orders)


class TestSettings:
    def test_unset_is_an_in_memory_source_reading_nothing_else_in_half_the_memory(
        self,
    ) -> None:
        config = duckdb_source.settings(lake())

        assert config == duckdb_source.DuckDBSettings(database=":memory:")
        assert config.in_memory
        assert config.allowed_paths == ()
        assert config.memory_limit == "50%"

    def test_allowed_paths_are_a_list_and_a_star_lifts_the_limit(self) -> None:
        assert duckdb_source.settings(
            lake(DUCKDB_ALLOWED_PATHS=" /data/a/ , s3://bucket/,")
        ).allowed_paths == ("/data/a/", "s3://bucket/")
        assert duckdb_source.settings(lake(DUCKDB_ALLOWED_PATHS="*")).allowed_paths is None

    def test_a_motherduck_database_is_not_limited_unless_asked(self) -> None:
        """Its data is remote, not in files beside the server."""
        motherduck = {"DUCKDB_DATABASE": "md:shop", "MOTHERDUCK_ACCESS_TOKEN": "token"}

        assert duckdb_source.settings(lake(**motherduck)).allowed_paths is None
        assert duckdb_source.settings(
            lake(**motherduck, DUCKDB_ALLOWED_PATHS="/data/")
        ).allowed_paths == ("/data/",)

    def test_a_sources_own_variable_wins_over_the_shared_one(self) -> None:
        source = DataSource(
            name="lake",
            dialect="duckdb",
            prefix="DB_LAKE_",
            environ={
                "DB_LAKE_DUCKDB_MEMORY_LIMIT": "1GB",
                "DUCKDB_MEMORY_LIMIT": "4GB",
                "DUCKDB_THREADS": "2",
            },
        )

        config = duckdb_source.settings(source)

        assert (config.memory_limit, config.threads) == ("1GB", 2)

    @pytest.mark.parametrize("raw", ["0", "two", "-1"])
    def test_a_count_must_be_a_whole_number_above_zero(self, raw: str) -> None:
        with pytest.raises(ExecutionUnavailableError, match="DB_LAKE_DUCKDB_THREADS is"):
            duckdb_source.settings(lake(DUCKDB_THREADS=raw))

    def test_the_setup_statements_are_never_shown(self) -> None:
        secret = "CREATE SECRET s (TYPE s3, KEY_ID 'k', SECRET 'hush')"

        assert "hush" not in repr(duckdb_source.settings(lake(DUCKDB_INIT_SQL=secret)))

    def test_the_setup_statements_decide_the_sources_identity(self) -> None:
        """They decide what its tables are (an attached catalog), so a cached
        result of other statements is another source's."""
        one = lake(DUCKDB_INIT_SQL="ATTACH IF NOT EXISTS 'one.duckdb' AS lake")
        two = lake(DUCKDB_INIT_SQL="ATTACH IF NOT EXISTS 'two.duckdb' AS lake")

        assert one.fingerprint() != two.fingerprint()

    def test_memory_threads_spill_and_paths_decide_the_identity_too(self) -> None:
        """A pool hands out connections opened with one set of them, and the
        paths decide what a query may return."""
        base = lake().fingerprint()

        for variable, setting in [
            ("DUCKDB_MEMORY_LIMIT", "64MB"),
            ("DUCKDB_THREADS", "2"),
            ("DUCKDB_SPILL_DIR", "off"),
            ("DUCKDB_SPILL_MAX_MB", "100"),
            ("DUCKDB_ALLOWED_PATHS", "*"),
        ]:
            assert lake(**{variable: setting}).fingerprint() != base, variable

    def test_pooled_connections_keep_each_sources_memory_limit(self) -> None:
        """Two sources alike but for their memory limit shared a pool, and the
        second was handed connections with the first one's limit."""
        limits = {}
        for name, limit in [("a", "64MiB"), ("b", "128MiB")]:
            source = DataSource(
                name=name,
                dialect="duckdb",
                prefix=f"DB_{name.upper()}_",
                environ={f"DB_{name.upper()}_DUCKDB_MEMORY_LIMIT": limit},
            )
            with db_router.get_connection(source) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT current_setting('memory_limit')")
                limits[name] = cursor.fetchall()[0][0]
        db_router.close_all_pools()

        assert limits == {"a": "64.0 MiB", "b": "128.0 MiB"}

    def test_the_listing_shows_no_setup_statements(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OBSL_DATABASES", "lake")
        monkeypatch.setenv("DB_LAKE_TYPE", "duckdb")
        monkeypatch.setenv("DB_LAKE_DUCKDB_INIT_SQL", "CREATE SECRET s (TYPE s3, SECRET 'hush')")

        assert "hush" not in repr(describe_datasources())


class TestSourceWithoutDatabaseFile:
    def test_it_reads_files_by_path(self, orders: str) -> None:
        """Opened read-only, as a database file is, an in-memory database
        refused to start: ``Cannot launch in-memory database in read-only mode``."""
        source = lake(DUCKDB_ALLOWED_PATHS=parts(orders))

        assert value(source, f"SELECT sum(amount) FROM '{orders}'") == 90

    def test_its_queries_share_one_instance(self) -> None:
        source = lake(DUCKDB_INIT_SQL="CREATE SEQUENCE IF NOT EXISTS seq")

        assert [value(source, "SELECT nextval('seq')") for _ in range(3)] == [1, 2, 3]

    def test_flight_and_rest_share_it(self) -> None:
        source = lake(DUCKDB_INIT_SQL="CREATE SEQUENCE IF NOT EXISTS seq")
        assert value(source, "SELECT nextval('seq')") == 1

        conn = db_router.connect(source)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT nextval('seq')")
            assert cursor.fetchall() == [(2,)]
        finally:
            conn.close()

    def test_every_connection_runs_the_setup_statements(self, orders: str) -> None:
        """``search_path`` holds per connection, and every query and every
        Flight cursor is a connection of its own."""
        source = lake(
            DUCKDB_INIT_SQL=(
                "CREATE SCHEMA IF NOT EXISTS sales; "
                f"CREATE OR REPLACE VIEW sales.orders AS SELECT * FROM '{orders}'; "
                "SET search_path = 'memory.sales'"
            ),
            DUCKDB_ALLOWED_PATHS=parts(orders),
        )

        assert [value(source, "SELECT count(*) FROM orders") for _ in range(2)] == [10, 10]
        conn = db_router.connect(source)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT count(*) FROM orders")
            assert cursor.fetchall() == [(10,)]
        finally:
            conn.close()

    def test_queries_at_once_each_get_a_connection(self, orders: str) -> None:
        """The first connections race to restrict the new instance: DuckDB
        refuses to set the allowed paths of one restricted in between."""
        source = lake(DUCKDB_ALLOWED_PATHS=parts(orders))
        results: list[Any] = []
        errors: list[Exception] = []

        def run() -> None:
            try:
                results.append(value(source, f"SELECT count(*) FROM '{orders}'"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert results == [10] * 8
        assert len(duckdb_source._instances) == 1

    def test_unset_the_memory_limit_is_half_the_memory(self) -> None:
        half = duckdb.connect(config={"memory_limit": "50%"})
        expected = half.sql("SELECT current_setting('memory_limit')").fetchall()[0][0]
        half.close()

        assert value(lake(), "SELECT current_setting('memory_limit')") == expected

    def test_setup_statements_of_connections_at_once_do_not_conflict(self) -> None:
        """Run at once, two ``CREATE OR REPLACE VIEW``s conflict in DuckDB's
        catalog: 14 of 16 connections failed with ``Catalog write-write
        conflict`` before setup ran one connection of an instance at a time.
        Flight's cursors are set up the same way."""
        source = lake(
            DUCKDB_INIT_SQL=(
                "CREATE SCHEMA IF NOT EXISTS s; "
                "CREATE OR REPLACE VIEW s.v AS SELECT 1 AS x; "
                "SET search_path = 'memory.s'"
            )
        )
        results: list[Any] = []
        errors: list[Exception] = []

        def rest() -> None:
            try:
                results.append(value(source, "SELECT x FROM v"))
            except Exception as exc:
                errors.append(exc)

        def flight() -> None:
            try:
                conn = db_router.connect(source)
                try:
                    cursor = conn.cursor()
                    cursor.execute("SELECT x FROM v")
                    results.append(cursor.fetchall()[0][0])
                finally:
                    conn.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=rest if i % 2 else flight) for i in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert results == [1] * 16

    def test_memory_and_threads_reach_duckdb(self) -> None:
        source = lake(DUCKDB_MEMORY_LIMIT="256MiB", DUCKDB_THREADS="2")

        assert value(source, "SELECT current_setting('memory_limit')") == "256.0 MiB"
        assert value(source, "SELECT current_setting('threads')") == 2

    def test_it_spills_into_a_directory_of_its_own_under_the_temp_directory(
        self, on_disk: None
    ) -> None:
        spilled = value(lake(), "SELECT current_setting('temp_directory')")

        assert os.path.dirname(spilled) == tempfile.gettempdir()
        assert os.path.basename(spilled).startswith("obsl-duckdb-")
        assert os.path.isdir(spilled)
        duckdb_source.close_instances()
        assert not os.path.exists(spilled)

    def test_it_spills_nothing_where_the_temp_directory_is_in_memory(self) -> None:
        with patch.object(federation, "_filesystem_type", return_value="tmpfs"):
            assert value(lake(), "SELECT current_setting('temp_directory')") == ""

    def test_a_spill_directory_and_its_limit(self, tmp_path: Any) -> None:
        source = lake(DUCKDB_SPILL_DIR=str(tmp_path / "spill"), DUCKDB_SPILL_MAX_MB="100")

        spilled = value(source, "SELECT current_setting('temp_directory')")

        assert os.path.dirname(spilled) == str(tmp_path / "spill")
        assert value(source, "SELECT current_setting('max_temp_directory_size')") == "100.0 MiB"

    def test_spilling_off(self, on_disk: None) -> None:
        source = lake(DUCKDB_SPILL_DIR="off")

        assert value(source, "SELECT current_setting('temp_directory')") == ""

    @pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs file modes")
    def test_a_spill_directory_that_cannot_be_written_is_named(self, tmp_path: Any) -> None:
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        try:
            with pytest.raises(ExecutionUnavailableError, match="DB_LAKE_DUCKDB_SPILL_DIR"):
                value(lake(DUCKDB_SPILL_DIR=str(locked / "spill")), "SELECT 1")
        finally:
            locked.chmod(0o700)

    def test_a_setting_duckdb_refuses_is_named(self) -> None:
        with pytest.raises(ExecutionUnavailableError, match="refused the settings of data source"):
            value(lake(DUCKDB_MEMORY_LIMIT="lots"), "SELECT 1")


class TestSetupStatementsFailing:
    def test_the_failure_names_the_variable_and_not_the_statements(self) -> None:
        init = "CREATE OR REPLACE SECRET s (TYPE s3, KEY_ID 'k', SECRET 'hush'); SELECT * FROM nope"

        with pytest.raises(ExecutionUnavailableError) as raised:
            value(lake(DUCKDB_INIT_SQL=init), "SELECT 1")

        message = str(raised.value)
        assert "DB_LAKE_DUCKDB_INIT_SQL" in message
        assert "nope" in message
        assert "hush" not in message
        assert raised.value.__cause__ is None and raised.value.__suppress_context__

    def test_a_parse_error_does_not_quote_them(self) -> None:
        """A parser error quotes the text around the error, which can be a key."""
        init = "CREATE SECRET s (TYPE s3, KEY_ID 'k', SECRET 'hush"

        with pytest.raises(ExecutionUnavailableError, match="could not parse") as raised:
            value(lake(DUCKDB_INIT_SQL=init), "SELECT 1")

        assert "hush" not in str(raised.value)

    def test_statements_that_cannot_run_twice_fail_on_the_second_connection(self) -> None:
        source = lake(DUCKDB_INIT_SQL="CREATE SEQUENCE seq")
        assert value(source, "SELECT nextval('seq')") == 1

        with pytest.raises(ExecutionUnavailableError, match="write them to run again"):
            value(source, "SELECT nextval('seq')")


class TestDatabaseFile:
    @pytest.fixture
    def shop(self, tmp_path: Any) -> str:
        path = str(tmp_path / "shop.duckdb")
        con = duckdb.connect(path)
        con.execute("CREATE TABLE shop AS SELECT 'eu' AS region")
        con.close()
        return path

    def test_every_connection_runs_the_setup_statements(self, shop: str) -> None:
        """Read-only, a file takes a TEMP view: one per connection."""
        source = lake(
            DUCKDB_DATABASE=shop,
            DUCKDB_INIT_SQL="CREATE OR REPLACE TEMP VIEW regions AS SELECT region FROM shop",
        )

        assert [value(source, "SELECT region FROM regions") for _ in range(2)] == ["eu", "eu"]

    def test_unset_it_spills_where_duckdb_does(self, shop: str) -> None:
        assert value(lake(DUCKDB_DATABASE=shop), "SELECT current_setting('temp_directory')") == (
            f"{shop}.tmp"
        )

    def test_a_spill_directory_holds_one_of_its_own_per_source(
        self, shop: str, tmp_path: Any
    ) -> None:
        source = lake(DUCKDB_DATABASE=shop, DUCKDB_SPILL_DIR=str(tmp_path / "spill"))

        spilled = value(source, "SELECT current_setting('temp_directory')")

        assert spilled == str(tmp_path / "spill" / f"obsl-duckdb-{source.fingerprint()}")

    def test_the_spill_limit_holds_in_duckdbs_own_directory_too(self, shop: str) -> None:
        source = lake(DUCKDB_DATABASE=shop, DUCKDB_SPILL_MAX_MB="100")

        assert value(source, "SELECT current_setting('max_temp_directory_size')") == "100.0 MiB"

    def test_sources_reading_one_file_at_once_must_agree_on_settings(self, shop: str) -> None:
        first = duckdb_source.connect(lake(DUCKDB_DATABASE=shop, DUCKDB_MEMORY_LIMIT="500MB"))
        try:
            with pytest.raises(ExecutionUnavailableError, match="DUCKDB_MEMORY_LIMIT"):
                duckdb_source.connect(lake(DUCKDB_DATABASE=shop, DUCKDB_MEMORY_LIMIT="600MB"))
        finally:
            first.close()


class TestModelOverFiles:
    def test_a_model_reads_a_parquet_glob_and_a_view_of_the_setup(
        self, orders: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One data object names its files in ``code``; another is a view the
        setup statements define. Neither needs a database file."""
        con = duckdb.connect()
        customers = tmp_path / "customers.csv"
        con.execute(
            f"COPY (SELECT * FROM (VALUES ('c0', 'Even'), ('c1', 'Odd')) t(code, name)) "
            f"TO '{customers}'"
        )
        con.close()
        for variable in ("DUCKDB_DATABASE", "DUCKDB_DATABASE_PATH", "DUCKDB_INIT_SQL"):
            monkeypatch.delenv(variable, raising=False)
        monkeypatch.setenv("OBSL_DATABASES", "lake")
        monkeypatch.setenv("DB_LAKE_TYPE", "duckdb")
        monkeypatch.setenv("DB_LAKE_DUCKDB_ALLOWED_PATHS", f"{parts(orders)},{customers}")
        monkeypatch.setenv(
            "DB_LAKE_DUCKDB_INIT_SQL",
            "CREATE SCHEMA IF NOT EXISTS crm; "
            f"CREATE OR REPLACE VIEW crm.customers AS SELECT * FROM '{customers}'",
        )
        model = f"""\
version: 1.0
settings:
  defaultDialect: duckdb
  defaultDataSource: lake
dataObjects:
  Orders:
    code: "{orders}"
    schema: ""
    columns:
      Order Id:
        code: id
        abstractType: int
        primaryKey: true
      Order Customer:
        code: customer
        abstractType: string
      Amount:
        code: amount
        abstractType: float
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer]
        columnsTo: [Customer Code]
  Customers:
    code: customers
    schema: crm
    columns:
      Customer Code:
        code: code
        abstractType: string
        primaryKey: true
      Customer Name:
        code: name
        abstractType: string
dimensions:
  Customer:
    dataObject: Customers
    column: Customer Name
    resultType: string
measures:
  Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum
"""
        query = QueryObject.model_validate(
            {
                "select": {"dimensions": ["Customer"], "measures": ["Revenue"]},
                "orderBy": [{"field": "Customer"}],
            }
        )

        _, result = _local.execute_query(model, query, None)

        assert result.rows == [["Even", 40.0], ["Odd", 50.0]]
        assert _local.validate(model, online=True).errors == []


class TestAllowedPaths:
    """A model names files in ``code``: a source reads only what it lists."""

    @pytest.fixture
    def secret(self, tmp_path: Any) -> str:
        path = tmp_path / "keys" / "key.json"
        path.parent.mkdir()
        path.write_text('{"private_key": "hush"}')
        return str(path)

    def test_unset_a_file_beside_the_server_is_refused_and_named(self, secret: str) -> None:
        with pytest.raises(ExecutionError) as raised:
            value(lake(), f'SELECT * FROM "{secret}"')

        assert "disabled by configuration" in str(raised.value)
        assert "DUCKDB_ALLOWED_PATHS" in str(raised.value)

    def test_a_database_file_still_reads_its_own_tables(self, tmp_path: Any, secret: str) -> None:
        shop = str(tmp_path / "shop.duckdb")
        con = duckdb.connect(shop)
        con.execute("CREATE TABLE shop AS SELECT 'eu' AS region")
        con.close()
        source = lake(DUCKDB_DATABASE=shop)

        assert value(source, "SELECT region FROM shop") == "eu"
        with pytest.raises(ExecutionError, match="disabled by configuration"):
            value(source, f'SELECT * FROM "{secret}"')

    def test_a_listed_directory_reads_and_its_neighbour_does_not(
        self, orders: str, tmp_path: Any
    ) -> None:
        neighbour = tmp_path / "parts_private"
        neighbour.mkdir()
        con = duckdb.connect()
        con.execute(f"COPY (SELECT 1 AS x) TO '{neighbour / 'x.parquet'}'")
        con.close()
        source = lake(DUCKDB_ALLOWED_PATHS=parts(orders))

        assert value(source, f"SELECT count(*) FROM '{orders}'") == 10
        with pytest.raises(ExecutionError, match="disabled by configuration"):
            value(source, f"SELECT * FROM '{neighbour / 'x.parquet'}'")
        with pytest.raises(ExecutionError, match="disabled by configuration"):
            value(source, f"SELECT * FROM '{parts(orders)}/../parts_private/x.parquet'")

    def test_a_listed_file_reads_and_its_neighbour_does_not(self, orders: str) -> None:
        source = lake(DUCKDB_ALLOWED_PATHS=f"{parts(orders)}/p0.parquet")

        assert value(source, f"SELECT count(*) FROM '{parts(orders)}/p0.parquet'") == 5
        with pytest.raises(ExecutionError, match="disabled by configuration"):
            value(source, f"SELECT count(*) FROM '{parts(orders)}/p1.parquet'")

    def test_a_star_reads_anything_the_server_can(self, secret: str) -> None:
        source = lake(DUCKDB_ALLOWED_PATHS="*")

        assert value(source, f'SELECT private_key FROM "{secret}"') == "hush"

    def test_flight_is_limited_too(self, secret: str) -> None:
        conn = db_router.connect(lake())
        try:
            cursor = conn.cursor()
            with pytest.raises(duckdb.PermissionException):
                cursor.execute(f'SELECT * FROM "{secret}"')
        finally:
            conn.close()

    @pytest.fixture
    def shop(self, tmp_path: Any) -> str:
        path = str(tmp_path / "shop.duckdb")
        con = duckdb.connect(path)
        con.execute("CREATE TABLE shop AS SELECT 'eu' AS region")
        con.close()
        return path

    def test_a_source_cannot_borrow_the_paths_of_another_on_a_shared_file(
        self, shop: str, secret: str
    ) -> None:
        """Queries reading one file at once share DuckDB's instance of it,
        limited to the paths of the first. Unrefused, source b read a key file
        it is refused on its own while source a, which lists it, held the file
        open."""
        a = lake(DUCKDB_DATABASE=shop, DUCKDB_ALLOWED_PATHS=os.path.dirname(secret))
        b = lake(DUCKDB_DATABASE=shop)

        held = duckdb_source.connect(a)
        try:
            with pytest.raises(ExecutionUnavailableError, match="other allowed paths"):
                duckdb_source.connect(b)
            # The same paths share it.
            duckdb_source.connect(a).close()
        finally:
            held.close()

        with pytest.raises(ExecutionError, match="disabled by configuration"):
            value(b, f'SELECT * FROM "{secret}"')

    def test_an_unlimited_source_cannot_share_a_file_another_limited(
        self, shop: str, secret: str
    ) -> None:
        limited = duckdb_source.connect(lake(DUCKDB_DATABASE=shop))
        try:
            with pytest.raises(ExecutionUnavailableError, match="other allowed paths"):
                duckdb_source.connect(lake(DUCKDB_DATABASE=shop, DUCKDB_ALLOWED_PATHS="*"))
        finally:
            limited.close()

    def test_the_limit_holds_on_every_later_connection(self, secret: str) -> None:
        """DuckDB's setting is the instance's, and cannot be turned back on."""
        source = lake(DUCKDB_INIT_SQL="SET enable_external_access = true")

        with pytest.raises(ExecutionUnavailableError, match="Cannot enable external access"):
            for _ in range(2):
                value(source, f'SELECT * FROM "{secret}"')
