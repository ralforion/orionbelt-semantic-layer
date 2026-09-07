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
