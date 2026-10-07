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

from collections.abc import Iterator
from typing import Any

import pytest

pytestmark = pytest.mark.adbc_flight

from tests.integration.test_adbc_flightsql import (  # noqa: E402
    MODEL_NAME,
    conn,  # noqa: F401
    flight_uri,  # noqa: F401
    tls_server,  # noqa: F401
)


def _sql_string(text: str) -> str:
    """*text* as a DuckDB string literal."""
    return "'" + text.replace("'", "''") + "'"


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

    def test_the_model_table_reads_with_a_star(self, conn: Any) -> None:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {MODEL_NAME}.model")
            table = cur.fetch_arrow_table()
        assert {"Customer Country", "Total Revenue", "Orders Count"} <= set(table.column_names)
        assert table.num_rows == 2

    def test_a_row_count_is_refused_as_documented(self, conn: Any) -> None:
        with conn.cursor() as cur, pytest.raises(Exception, match="count measure"):
            cur.execute(f"SELECT COUNT(*) FROM {MODEL_NAME}.model")
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

    @pytest.fixture
    def duckdb_with_adbc(self) -> Iterator[Any]:
        """A DuckDB connection with ``adbc_scanner`` loaded, closed explicitly.

        Explicitly, not by dropping the last reference: DuckDB 1.5.6 frees a
        connection holding an ``adbc_scanner`` connection with the GIL held,
        and the extension's cleanup waits on the Flight SQL server. That server
        runs in this process, its handlers need the GIL, and the test hung
        once it returned. ``close()`` takes the path that does not wait.
        """
        duckdb = pytest.importorskip("duckdb", reason="duckdb required")
        connection = duckdb.connect()
        try:
            try:
                connection.execute("INSTALL adbc_scanner FROM community")
                connection.execute("LOAD adbc_scanner")
            except Exception as exc:  # noqa: BLE001 - network or unavailable build
                pytest.skip(f"adbc_scanner community extension unavailable: {exc}")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _attach(connection: Any, uri: str, **options: str) -> None:
        """The page's ``ATTACH`` statement, naming the database ``obsl``.

        ATTACH options cannot be bound parameters, so the recipe's literals
        are filled in here, quoted as SQL strings.
        """
        driver = pytest.importorskip("adbc_driver_flightsql")._driver_path()
        extra = "".join(f', "{key}" {_sql_string(value)}' for key, value in options.items())
        connection.execute(
            f"ATTACH '' AS obsl (TYPE adbc, driver {_sql_string(driver)}, "
            f"uri {_sql_string(uri)}{extra})"
        )

    @pytest.fixture
    def adbc_duckdb(self, duckdb_with_adbc: Any, flight_uri: str) -> Any:
        """``duckdb_with_adbc`` with the test server attached as ``obsl``."""
        self._attach(duckdb_with_adbc, flight_uri)
        return duckdb_with_adbc

    def test_a_duckdb_shell_queries_the_model(self, adbc_duckdb: Any) -> None:
        rows = adbc_duckdb.execute(
            "SELECT * FROM adbc_scan('obsl', ?)",
            [f'SELECT "Customer Country", "Total Revenue" FROM {MODEL_NAME}'],
        ).fetchall()
        assert {r[0] for r in rows} == {"US", "UK"}
        assert all(isinstance(r[1], float) for r in rows)

    def test_the_attached_model_answers_a_named_column_list(self, adbc_duckdb: Any) -> None:
        rows = adbc_duckdb.execute(
            f'SELECT "Customer Country", "Total Revenue" FROM obsl.{MODEL_NAME}.model'
        ).fetchall()
        assert {r[0] for r in rows} == {"US", "UK"}

    @pytest.mark.parametrize(
        "statement",
        [
            f"SELECT * FROM obsl.{MODEL_NAME}.model",
            f"SELECT * FROM adbc_scan_table('obsl', 'model', schema := '{MODEL_NAME}')",
        ],
    )
    def test_the_attached_model_reads_with_a_star(self, adbc_duckdb: Any, statement: str) -> None:
        """Both read every announced column; before, both failed on a
        column-count mismatch."""
        relation = adbc_duckdb.execute(statement)
        columns = [d[0] for d in relation.description]
        rows = relation.fetchall()
        assert {"Customer Country", "Total Revenue", "Orders Count"} <= set(columns)
        assert {r[0] for r in rows} == {"US", "UK"}

    def test_the_catalog_functions_answer(self, adbc_duckdb: Any) -> None:
        """``adbc_tables`` is the one that failed before #433: DuckDB asks for
        the four-column ``CommandGetTables`` shape and OBSL always sent five."""
        tables = adbc_duckdb.execute("SELECT * FROM adbc_tables('obsl')").fetchall()
        assert "model" in {r[2] for r in tables}

        columns = adbc_duckdb.execute(
            "SELECT * FROM adbc_schema('obsl', 'model', schema := ?) LIMIT 20",
            [MODEL_NAME],
        ).fetchall()
        assert "Customer Country" in {r[0] for r in columns}

    def test_tls_trust_goes_in_attach_options(
        self, duckdb_with_adbc: Any, tls_server: tuple[str, bytes]
    ) -> None:
        """The TLS section's DuckDB form: the driver option as an ATTACH option."""
        uri, cert_pem = tls_server
        self._attach(
            duckdb_with_adbc,
            uri,
            **{"adbc.flight.sql.client_option.tls_root_certs": cert_pem.decode()},
        )
        rows = duckdb_with_adbc.execute(
            "SELECT * FROM adbc_scan('obsl', ?)",
            [f'SELECT "Customer Country", "Total Revenue" FROM {MODEL_NAME}'],
        ).fetchall()
        assert len(rows) == 2

    def test_the_result_composes_with_local_sql(self, adbc_duckdb: Any) -> None:
        """The page claims filtering, aggregating and CREATE TABLE AS."""
        query = f'SELECT "Customer Country", "Total Revenue" FROM {MODEL_NAME}'

        total = adbc_duckdb.execute(
            "SELECT sum(\"Total Revenue\") FROM adbc_scan('obsl', ?)", [query]
        ).fetchone()[0]
        adbc_duckdb.execute("CREATE TABLE local AS SELECT * FROM adbc_scan('obsl', ?)", [query])
        materialised = adbc_duckdb.execute('SELECT sum("Total Revenue") FROM local').fetchone()[0]
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
