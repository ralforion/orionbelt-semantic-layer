"""Stage 1: Dremio talks to OBSL via the Postgres wire protocol.

Asserts that:

1. The Postgres source registers cleanly (no metadata-probe error from
   Dremio against OBSL's pg_catalog emulation).
2. Dremio can list at least one table under the source — proving the
   ``information_schema.tables`` round-trip works.
3. A semantic query pushed through the source returns non-empty rows.

The model exposed by OBSL is the ``commerce`` demo model (overridable via
``OBSL_MODEL_NAME``; see ``conftest.py``). The pgwire ``database``
parameter resolves to the OBML model name, OBSL surfaces one schema per
loaded model (named after the model), and each schema holds a single
virtual table called ``model``. So the fully-qualified Dremio path is::

    obsl_pg."commerce".model
"""

from __future__ import annotations

import pytest

from tests.integration.dremio.conftest import (
    DREMIO_SOURCE_NAME,
    OBSL_MODEL_NAME,
    DremioSession,
    RunSql,
)

pytestmark = pytest.mark.dremio

# pgwire endpoint exposed by the docker-compose for the OBSL container.
# Inside the compose network OBSL is reachable as ``obsl:5432``; from the
# host (where pytest runs) the compose file publishes the same port on
# localhost. The Dremio relay path is the original Stage-1 contract;
# this direct connection is only used by the OBSQL EXISTS round-trip
# since Dremio's parser can't carry the EXISTS subquery body.
import os  # noqa: E402

OBSL_PGWIRE_HOST_LOCAL = os.environ.get("OBSL_PGWIRE_HOST_LOCAL", "localhost")
OBSL_PGWIRE_PORT_LOCAL = int(os.environ.get("OBSL_PGWIRE_PORT_LOCAL", "15432"))


def test_source_registers(dremio_session: DremioSession) -> None:
    """The conftest fixture creates the source — landing here proves it succeeded.

    A failure during ``_ensure_postgres_source`` raises before the test
    runs, surfacing the real Dremio error message rather than a generic
    'fixture failed' message.
    """

    assert dremio_session.source_name == DREMIO_SOURCE_NAME
    assert dremio_session.token


def test_information_schema_lists_model_table(run_dremio_sql: RunSql) -> None:
    """Dremio's own INFORMATION_SCHEMA surfaces the OBSL model as a dataset.

    Dremio exposes ``INFORMATION_SCHEMA`` at the root (not under each
    source). With OBSL's v2.5.0 catalog layout the model lives at
    ``<source>.<model>.model``, so we look for ``TABLE_NAME='model'``
    in any schema under the OBSL Postgres source.
    """

    rows = run_dremio_sql(
        'SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA."TABLES" '
        f"WHERE TABLE_SCHEMA = '{DREMIO_SOURCE_NAME}.{OBSL_MODEL_NAME}' "
        "AND TABLE_NAME = 'model'"
    )
    assert rows, (
        f"Dremio could not see the OBSL model at "
        f"'{DREMIO_SOURCE_NAME}.{OBSL_MODEL_NAME}.model' via INFORMATION_SCHEMA"
    )


def test_semantic_query_round_trip(run_dremio_sql: RunSql) -> None:
    """A real OBSQL query pushed through Dremio returns rows from DuckDB."""

    rows = run_dremio_sql(
        'SELECT "Client Name", "Total Sales" '
        f'FROM {DREMIO_SOURCE_NAME}."{OBSL_MODEL_NAME}".model '
        "LIMIT 5"
    )
    assert rows, "OBSL returned no rows for the dim+measure query"
    assert len(rows[0]) == 2


def test_exists_round_trip_via_pgwire(dremio_session: DremioSession) -> None:
    """OBSQL ``EXISTS (SELECT 1 FROM <DataObject>)`` end-to-end via OBSL
    pgwire directly (v2.7.5+).

    Until v2.7.5 the OBSQL translator rejected EXISTS / NOT EXISTS even
    though the underlying QueryObject layer accepted them. This test
    pins the full path with a *direct* pgwire connection to OBSL —
    Dremio's relay can't carry this query because Dremio's own SQL
    parser doesn't know about the data objects nested inside OBSL's
    model (it only sees the single virtual ``model`` table), so the
    EXISTS body's ``FROM "Sales"`` would be rejected before OBSL ever
    gets to translate it. This is a fundamental limitation of the
    Postgres-federation path, not an OBSL bug.

    The OBSL test container is reachable on ``localhost:5432`` (the
    docker-compose exposes the pgwire port). We use psycopg directly.
    """
    psycopg = pytest.importorskip("psycopg", reason="psycopg required for pgwire test")
    with (
        psycopg.connect(
            host=OBSL_PGWIRE_HOST_LOCAL,
            port=OBSL_PGWIRE_PORT_LOCAL,
            user="obsl",
            password="trust",
            dbname=OBSL_MODEL_NAME,
            sslmode="disable",
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            'SELECT "Client Name", "Total Sales" '
            "FROM model "
            'WHERE EXISTS (SELECT 1 FROM "Sales") '
            'ORDER BY "Total Sales" DESC '
            "LIMIT 5"
        )
        exists_rows = cur.fetchall()
        cur.execute(
            'SELECT "Client Name" '
            "FROM model "
            'WHERE NOT EXISTS (SELECT 1 FROM "Client Complaints") '
            'ORDER BY "Client Name" '
            "LIMIT 5"
        )
        nonexists_rows = cur.fetchall()
    assert exists_rows, "OBSQL EXISTS returned no rows from DuckDB via pgwire"
    assert len(exists_rows[0]) == 2
    assert nonexists_rows, "OBSQL NOT EXISTS returned no rows"
    assert len(nonexists_rows[0]) == 1
