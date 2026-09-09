"""Every claim `docs/guide/adbc.md` makes, run against a real ADBC client.

The page tells people what to type. A documented recipe that does not run is
worse than no page: it costs the reader the time to find out. So the samples
live here too, phrased as assertions.
"""

# ruff: noqa: F811 — pytest fixtures are imported by name and then shadowed by
# the parameters that request them, which is how sharing a fixture across
# modules works. The alternative is a conftest, which would move the harness
# away from the suite that owns it.
from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.adbc_flight

from tests.integration.test_adbc_flightsql import (  # noqa: E402
    MODEL_NAME,
    conn,  # noqa: F401
    flight_uri,  # noqa: F401
)


class TestTheConnectRecipe:
    def test_the_first_sample_runs(self, conn: Any) -> None:
        """The `Connect` block, verbatim."""
        with conn.cursor() as cur:
            cur.execute(f'SELECT "Customer Country", "Total Revenue" FROM {MODEL_NAME}')
            table = cur.fetch_arrow_table()
        assert table.num_rows > 0
        assert table.column_names == ["Customer Country", "Total Revenue"]

    def test_model_selection_by_call_header(self, flight_uri: str) -> None:
        """The `Picking a model` block."""
        from adbc_driver_flightsql import dbapi

        connection = dbapi.connect(
            flight_uri,
            db_kwargs={"adbc.flight.sql.rpc.call_header.x-obsl-model": MODEL_NAME},
        )
        try:
            with connection.cursor() as cur:
                cur.execute(f'SELECT "Customer Country" FROM {MODEL_NAME}')
                assert cur.fetch_arrow_table().num_rows > 0
        finally:
            connection.close()


class TestWhatYouCanSend:
    def test_the_obsql_sample(self, conn: Any) -> None:
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT "Customer Country", "Total Revenue" FROM {MODEL_NAME} '
                "WHERE \"Customer Country\" = 'US'"
            )
            table = cur.fetch_arrow_table()
        assert table.column("Customer Country").to_pylist() == ["US"]

    def test_the_raw_column_sample(self, conn: Any) -> None:
        with conn.cursor() as cur:
            cur.execute(f'SELECT "Orders"."Amount" FROM {MODEL_NAME}')
            table = cur.fetch_arrow_table()
        assert table.column_names == ["Orders.Amount"]

    def test_select_star_is_refused_as_documented(self, conn: Any) -> None:
        with conn.cursor() as cur, pytest.raises(Exception, match="(?i)reject|unsupported|\\*"):
            cur.execute(f"SELECT * FROM {MODEL_NAME}")
            cur.fetch_arrow_table()

    def test_an_unknown_relation_errors_rather_than_returning_nothing(self, conn: Any) -> None:
        """The page promises a client can tell 'nothing matched' from
        'not allowed'."""
        with conn.cursor() as cur, pytest.raises(Exception, match="(?i)error|reject|unknown|not"):
            cur.execute("SELECT x FROM nonexistent_relation_xyz")
            cur.fetch_arrow_table()


class TestThePreparedStatementSample:
    def test_it_runs_and_rebinds(self, conn: Any) -> None:
        sql = (
            f'SELECT "Customer Country", "Total Revenue" FROM {MODEL_NAME} '
            'WHERE "Customer Country" = ?'
        )
        with conn.cursor() as cur:
            parameters = cur.adbc_prepare(sql)
            assert parameters is not None
            assert str(parameters.field(0).type) == "string"
            cur.execute(sql, parameters=("US",))
            us = cur.fetch_arrow_table()
            cur.execute(sql, parameters=("__none__",))
            none = cur.fetch_arrow_table()
        assert us.num_rows == 1
        assert none.num_rows == 0

    def test_the_documented_parameter_name_is_dollar_one(self, conn: Any) -> None:
        """The page prints ``$1: string``."""
        with conn.cursor() as cur:
            parameters = cur.adbc_prepare(
                f'SELECT "Customer Country" FROM {MODEL_NAME} WHERE "Customer Country" = ?'
            )
        assert parameters.names == ["$1"]


class TestTheCatalogSamples:
    def test_the_two_catalog_statements_run(self, conn: Any) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_type = 'VIEW'"
            )
            views = cur.fetch_arrow_table()
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE ordinal_position > ?",
                parameters=(1,),
            )
            columns = cur.fetch_arrow_table()
        assert views.column_names == ["table_name"]
        assert columns.column_names == ["column_name"]

    @pytest.mark.parametrize("statement", ["SHOW TABLES", "DESCRIBE " + MODEL_NAME])
    def test_the_bi_tool_forms_answer(self, conn: Any, statement: str) -> None:
        with conn.cursor() as cur:
            cur.execute(statement)
            assert cur.fetch_arrow_table().num_rows > 0

    def test_an_unsupported_predicate_does_not_fail_the_query(self, conn: Any) -> None:
        """The page says it is left unfiltered rather than failing."""
        with conn.cursor() as cur:
            cur.execute("SELECT table_name FROM information_schema.tables WHERE weird(x) = 1")
            assert cur.fetch_arrow_table().num_rows > 0


class TestTheDuckDBRecipe:
    """The `From DuckDB` section, run as written.

    Skipped where the community extension cannot be installed - it is fetched
    over the network, unlike everything else this suite needs. The recipe is
    the one claim on the page that depends on a third-party extension, so it
    gets a skip rather than a hard requirement.
    """

    @staticmethod
    def _duckdb_with_adbc() -> Any:
        duckdb = pytest.importorskip("duckdb", reason="duckdb required")
        connection = duckdb.connect()
        try:
            connection.execute("INSTALL adbc_scanner FROM community")
            connection.execute("LOAD adbc_scanner")
        except Exception as exc:  # noqa: BLE001 - network or unavailable build
            pytest.skip(f"adbc_scanner community extension unavailable: {exc}")
        return connection

    @staticmethod
    def _handle(connection: Any, uri: str) -> Any:
        driver = pytest.importorskip("adbc_driver_flightsql")._driver_path()
        return connection.execute(
            "SELECT adbc_connect(MAP {'driver': ?, 'uri': ?})", [driver, uri]
        ).fetchone()[0]

    def test_a_duckdb_shell_queries_the_model(self, flight_uri: str) -> None:
        connection = self._duckdb_with_adbc()
        handle = self._handle(connection, flight_uri)
        rows = connection.execute(
            "SELECT * FROM adbc_scan(?, ?)",
            [handle, f'SELECT "Customer Country", "Total Revenue" FROM {MODEL_NAME}'],
        ).fetchall()
        assert {r[0] for r in rows} == {"US", "UK"}
        assert all(isinstance(r[1], float) for r in rows)

    def test_the_catalog_functions_answer(self, flight_uri: str) -> None:
        """``adbc_tables`` is the one that failed before #433: DuckDB asks for
        the four-column ``CommandGetTables`` shape and OBSL always sent five."""
        connection = self._duckdb_with_adbc()
        handle = self._handle(connection, flight_uri)
        tables = connection.execute("SELECT * FROM adbc_tables(?)", [handle]).fetchall()
        assert "model" in {r[2] for r in tables}

        columns = connection.execute(
            "SELECT * FROM adbc_schema(?, 'model', schema := ?) LIMIT 20",
            [handle, MODEL_NAME],
        ).fetchall()
        assert "Customer Country" in {r[0] for r in columns}

    def test_the_result_composes_with_local_sql(self, flight_uri: str) -> None:
        """The page claims filtering, aggregating and CREATE TABLE AS."""
        connection = self._duckdb_with_adbc()
        handle = self._handle(connection, flight_uri)
        query = f'SELECT "Customer Country", "Total Revenue" FROM {MODEL_NAME}'

        total = connection.execute(
            'SELECT sum("Total Revenue") FROM adbc_scan(?, ?)', [handle, query]
        ).fetchone()[0]
        connection.execute("CREATE TABLE local AS SELECT * FROM adbc_scan(?, ?)", [handle, query])
        materialised = connection.execute('SELECT sum("Total Revenue") FROM local').fetchone()[0]
        assert materialised == total


class TestThePageItselfRuns:
    """Executes the guide's code blocks verbatim, rather than transcribing them.

    Transcribing is what let the prepared-statement sample ship broken: it
    passed ``...`` where the SQL belongs - the Ellipsis object, not a
    statement - while the test beside it used a real ``sql`` variable and
    passed. Two texts claiming to be the same thing, only one of them checked.

    Only blocks that drive an existing connection are run. The ones that call
    ``dbapi.connect`` name a fixed host and port that no test server listens
    on, and rewriting them here would put the transcription back.
    """

    GUIDE = "docs/guide/adbc.md"

    def _runnable_blocks(self) -> list[str]:
        import pathlib
        import re

        text = (pathlib.Path(__file__).resolve().parents[2] / self.GUIDE).read_text()
        blocks = re.findall(r"```python\n(.*?)```", text, re.S)
        return [b for b in blocks if "conn.cursor()" in b and "dbapi.connect" not in b]

    def test_there_are_blocks_to_run(self) -> None:
        """Guards the guard: a renamed page or fence would pass everything."""
        assert self._runnable_blocks(), f"no runnable python blocks found in {self.GUIDE}"

    def test_every_runnable_block_executes(self, conn: Any) -> None:
        for index, block in enumerate(self._runnable_blocks()):
            try:
                exec(compile(block, f"{self.GUIDE}#block{index}", "exec"), {"conn": conn})
            except Exception as exc:  # noqa: BLE001 — the failure *is* the result
                pytest.fail(f"{self.GUIDE} block {index} does not run: {exc}\n\n{block}")
