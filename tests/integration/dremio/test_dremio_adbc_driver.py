"""The ob-dremio driver against a live Dremio, over ADBC Flight SQL.

Track I-2 of ``design/PLAN_adbc.md``: Dremio is the pilot for OBSL as an
ADBC *client*. It was the obvious first dialect because it is the only one
whose driver was a hand-rolled Flight client — so the migration deletes
protocol code rather than swapping one vendor SDK for another, and it
exercises ``adbc_driver_flightsql``, the same dependency Track II's clients
use to reach OBSL.

These talk straight to Dremio's Flight SQL port; nothing here goes through
OBSL. ``test_dremio_full_circle.py`` covers the other direction, where OBSL
compiles to the Dremio dialect and executes through this driver.

Backing data is Dremio's own ``INFORMATION_SCHEMA``, always present, so no
dataset promotion is needed.

Run with the stack from this directory up::

    tests/integration/dremio/run.sh
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from tests.integration.dremio.conftest import (
    DREMIO_ADMIN_PASS,
    DREMIO_ADMIN_USER,
    DREMIO_FLIGHT_HOST,
    DREMIO_FLIGHT_PORT,
)

pytestmark = pytest.mark.dremio

# A row source that exists in every Dremio, with a column of each type that
# the migration had to keep answering identically.
TYPED_QUERY = (
    "SELECT CAST(1 AS INT) AS i, CAST(2 AS BIGINT) AS bi, CAST(1.5 AS DOUBLE) AS d, "
    "CAST('x' AS VARCHAR) AS s, CAST('2020-01-02' AS DATE) AS dt, "
    "CAST('2020-01-02 03:04:05' AS TIMESTAMP) AS ts, "
    "CAST(12.34 AS DECIMAL(18,2)) AS dcm, true AS b "
    "FROM (VALUES(1))"
)


@pytest.fixture(scope="module")
def dremio_conn(dremio_admin_token: str) -> Iterator[Any]:
    """An ``ob_dremio`` connection to the live container.

    Depends on ``dremio_admin_token`` rather than ``dremio_session``: the
    admin user has to exist before Flight will authenticate anyone, but the
    OBSL Postgres source is irrelevant here.
    """
    ob_dremio = pytest.importorskip("ob_dremio", reason="ob-dremio not installed")

    connection = ob_dremio.connect(
        host=DREMIO_FLIGHT_HOST,
        port=DREMIO_FLIGHT_PORT,
        username=DREMIO_ADMIN_USER,
        password=DREMIO_ADMIN_PASS,
    )
    try:
        yield connection
    finally:
        connection.close()


class TestItIsActuallyADBC:
    def test_the_connection_is_an_adbc_connection(self, dremio_conn: Any) -> None:
        """Guards the point of the exercise: no hand-rolled Flight client.

        Every other test here would pass just as well against the old
        ``pyarrow.flight`` implementation — that is the intent, since the
        migration is meant to be invisible — so one test has to check what
        is underneath.
        """
        import adbc_driver_manager.dbapi

        native = dremio_conn._native
        assert isinstance(native, adbc_driver_manager.dbapi.Connection), (
            f"native connection is {type(native).__module__}.{type(native).__name__}"
        )

    def test_the_adbc_driver_behind_it_is_flightsql(self, dremio_conn: Any) -> None:
        """``dbapi.Connection`` is the manager's shared class, so the type
        alone does not say which driver loaded. Errors carry the driver's
        own tag, and only one of them says ``[FlightSQL]``."""
        from adbc_driver_manager import NotSupportedError

        with pytest.raises(NotSupportedError, match=r"\[FlightSQL\]"):
            dremio_conn._native.adbc_get_statistic_names()

    def test_authentication_happened(self, dremio_conn: Any) -> None:
        """Dremio refuses unauthenticated Flight calls, so a row proves the
        driver ran the basic-token exchange and reused the bearer."""
        with dremio_conn.cursor() as cur:
            cur.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.COLUMNS LIMIT 1")
            assert len(cur.fetchall()) == 1


class TestConnectIsQuiet:
    def test_connecting_emits_no_warning(self, dremio_admin_token: str) -> None:
        """ADBC disables autocommit on connect unless told not to bother, and
        Dremio -- which has no transactions -- refuses, so every connection
        used to warn that it "will not be DB-API 2.0 compliant".

        Opens its own connection, since the warning happens at connect and
        the shared ``dremio_conn`` is already past it. It still takes
        ``dremio_admin_token``: that fixture carries the suite's
        reachability skip, and a test that connects without it fails with
        "connection refused" wherever the stack is not up.
        """
        import warnings

        import ob_dremio

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            connection = ob_dremio.connect(
                host=DREMIO_FLIGHT_HOST,
                port=DREMIO_FLIGHT_PORT,
                username=DREMIO_ADMIN_USER,
                password=DREMIO_ADMIN_PASS,
            )
            connection.close()
        assert [str(w.message) for w in caught] == []


class TestPep249Surface:
    """The published surface has to survive the swap unchanged."""

    def test_rowcount_is_the_row_count(self, dremio_conn: Any) -> None:
        with dremio_conn.cursor() as cur:
            cur.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.COLUMNS LIMIT 3")
            assert cur.rowcount == 3

    def test_description_reports_pep249_type_constants(self, dremio_conn: Any) -> None:
        """Not PyArrow ``DataType`` objects, which is what ADBC's own cursor
        puts in that slot — ``db_executor`` maps this to a type hint."""
        from ob_dremio.type_codes import DATETIME, NUMBER, STRING

        with dremio_conn.cursor() as cur:
            cur.execute(TYPED_QUERY)
            by_name = {d[0]: d[1] for d in cur.description}
        assert by_name["i"] == NUMBER
        assert by_name["dcm"] == NUMBER
        assert by_name["s"] == STRING
        assert by_name["dt"] == DATETIME
        assert by_name["ts"] == DATETIME

    def test_fetch_methods_walk_the_result_once(self, dremio_conn: Any) -> None:
        """Each fetch consumes what it returns, and the cursor then empties."""
        with dremio_conn.cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS ORDER BY COLUMN_NAME LIMIT 3"
            )
            first = cur.fetchone()
            middle = cur.fetchmany(1)
            rest = cur.fetchall()
            exhausted = cur.fetchone()
        assert isinstance(first, tuple)
        assert len(middle) == 1
        assert len(rest) == 1
        assert exhausted is None
        assert len({first, middle[0], rest[0]}) == 3, "a row was handed out twice"

    def test_iteration_yields_rows(self, dremio_conn: Any) -> None:
        with dremio_conn.cursor() as cur:
            cur.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.COLUMNS LIMIT 2")
            assert len([row for row in cur]) == 2


class TestArrowFidelity:
    """The types OBSL's reconciliation layer is written against."""

    def test_arrow_types_are_what_dremio_always_returned(self, dremio_conn: Any) -> None:
        with dremio_conn.cursor() as cur:
            cur.execute(TYPED_QUERY)
            table = cur.fetch_arrow_table()
        types = {f.name: str(f.type) for f in table.schema}
        assert types == {
            "i": "int32",
            "bi": "int64",
            "d": "double",
            "s": "string",
            # date64, not date32 — the narrowing OBSL applies downstream
            # is written against exactly this.
            "dt": "date64[ms]",
            "ts": "timestamp[ms]",
            "dcm": "decimal128(18, 2)",
            "b": "bool",
        }

    def test_values_survive_the_round_trip(self, dremio_conn: Any) -> None:
        import datetime
        from decimal import Decimal

        with dremio_conn.cursor() as cur:
            cur.execute(TYPED_QUERY)
            row = cur.fetch_arrow_table().to_pylist()[0]
        assert row["dcm"] == Decimal("12.34")
        assert row["dt"] == datetime.date(2020, 1, 2)
        assert row["ts"] == datetime.datetime(2020, 1, 2, 3, 4, 5)

    def test_an_empty_result_keeps_its_schema(self, dremio_conn: Any) -> None:
        """A null-typed empty column is the bug that broke Flight cache hits."""
        with dremio_conn.cursor() as cur:
            cur.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE 1=0")
            table = cur.fetch_arrow_table()
        assert table.num_rows == 0
        assert str(table.schema.field("TABLE_NAME").type) == "string"


class TestParameterBinding:
    """New with ADBC. The hand-rolled client had nowhere to put a value, so
    it sent the statement with its placeholders intact and Dremio answered
    with a Calcite ``RexDynamicParam`` internal error.
    """

    def test_a_bound_value_filters(self, dremio_conn: Any) -> None:
        with dremio_conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ? LIMIT 5",
                ("COLUMNS",),
            )
            rows = cur.fetchall()
        assert rows, "bound filter matched nothing"
        assert {r[0] for r in rows} == {"COLUMNS"}

    def test_rebinding_gives_a_different_answer(self, dremio_conn: Any) -> None:
        """The regression that decided how ``_execute_sql`` is written.

        Reusing one native cursor makes ADBC skip re-preparing when the SQL
        text is unchanged, and Dremio then returns the *first* execution's
        rows for the second binding - the same number, no error. This test
        fails with a stale answer rather than an exception, which is why the
        driver opens a statement per execution.
        """
        sql = "SELECT SUM(ORDINAL_POSITION) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ?"
        with dremio_conn.cursor() as cur:
            cur.execute(sql, ("COLUMNS",))
            columns = cur.fetchone()[0]
            cur.execute(sql, ("__no_such_table__",))
            missing = cur.fetchone()[0]
        assert columns > 0
        assert missing is None, "no rows matched, so SUM has nothing to add"

    def test_count_in_a_prepared_statement_is_refused_by_dremio(self, dremio_conn: Any) -> None:
        """A live limitation, not a driver defect — and Dremio's, not ours.

        Dremio describes a prepared ``COUNT`` as ``int64`` NOT NULL and then
        streams it nullable. ADBC compares the two and refuses the endpoint;
        the hand-rolled client never compared, but it also could not bind a
        parameter at all, so nothing that works today stops working.

        Exactly the defect class the OBSL Flight server was caught in by its
        own ADBC harness: catalog schemas marked every field nullable where
        the spec says NOT NULL, and only ADBC noticed.

        Aggregates that are nullable on both sides (``SUM``, ``MAX``) are
        fine, as is an unparameterised ``COUNT``.
        """
        from adbc_driver_manager import OperationalError

        with (
            pytest.raises(OperationalError, match="(?i)inconsistent schema"),
            dremio_conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ?",
                ("COLUMNS",),
            )

    def test_an_unparameterised_count_is_unaffected(self, dremio_conn: Any) -> None:
        """The shape OBSL itself emits: literals compiled in, no placeholders."""
        with dremio_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = 'COLUMNS'"
            )
            assert cur.fetchone()[0] > 0

    def test_executemany_runs_one_statement_per_parameter_set(self, dremio_conn: Any) -> None:
        """ADBC's own ``executemany`` binds the batch over ``DoPut``, which
        Dremio refuses with ``acceptPut is not implemented`` — so this
        driver loops instead."""
        with dremio_conn.cursor() as cur:
            cur.executemany(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ?",
                [("COLUMNS",), ("TABLES",)],
            )


class TestErrors:
    def test_a_bad_statement_raises(self, dremio_conn: Any) -> None:
        with (
            pytest.raises(Exception, match="(?i)not found|no_such|validation|table"),
            dremio_conn.cursor() as cur,
        ):
            cur.execute("SELECT * FROM no_such_table_xyz")
            cur.fetchall()

    def test_a_closed_connection_refuses_a_cursor(self, dremio_conn: Any) -> None:
        import ob_dremio
        from ob_dremio.exceptions import ProgrammingError

        connection = ob_dremio.connect(
            host=DREMIO_FLIGHT_HOST,
            port=DREMIO_FLIGHT_PORT,
            username=DREMIO_ADMIN_USER,
            password=DREMIO_ADMIN_PASS,
        )
        connection.close()
        with pytest.raises(ProgrammingError, match="closed"):
            connection.cursor()
