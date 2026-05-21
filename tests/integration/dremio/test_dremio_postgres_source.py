"""Stage 1: Dremio talks to OBSL via the Postgres wire protocol.

Asserts that:

1. The Postgres source registers cleanly (no metadata-probe error from
   Dremio against OBSL's pg_catalog emulation).
2. Dremio can list at least one table under the source — proving the
   ``information_schema.tables`` round-trip works.
3. A semantic query pushed through the source returns non-empty rows.

The model exposed by OBSL is the baked-in ``orionbelt_1_commerce`` demo
(see ``examples/orionbelt_1_commerce.yaml``). The pgwire ``database``
parameter resolves to the OBML model name (filename stem fallback), and
OBSL surfaces a single virtual table per model under the ``orionbelt``
schema. So the fully-qualified Dremio path is::

    obsl_pg."orionbelt"."orionbelt_1_commerce"
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
    source). We filter by ``TABLE_SCHEMA`` to find any path that lands
    in the OBSL Postgres source's ``orionbelt`` namespace.
    """

    rows = run_dremio_sql(
        'SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA."TABLES" '
        f"WHERE TABLE_SCHEMA LIKE '{DREMIO_SOURCE_NAME}.%' "
        f"AND TABLE_NAME = '{OBSL_MODEL_NAME}'"
    )
    assert rows, (
        "Dremio could not see the OBSL model dataset under "
        f"'{DREMIO_SOURCE_NAME}.orionbelt' via INFORMATION_SCHEMA"
    )
    schemas = {str(row[0]) for row in rows}
    assert any(s.endswith(".orionbelt") for s in schemas), (
        f"expected '{DREMIO_SOURCE_NAME}.orionbelt' schema, got {schemas!r}"
    )


def test_semantic_query_round_trip(run_dremio_sql: RunSql) -> None:
    """A real OBSQL query pushed through Dremio returns rows from DuckDB."""

    rows = run_dremio_sql(
        'SELECT "Client Name", "Total Sales" '
        f'FROM {DREMIO_SOURCE_NAME}.orionbelt."{OBSL_MODEL_NAME}" '
        "LIMIT 5"
    )
    assert rows, "OBSL returned no rows for the dim+measure query"
    assert len(rows[0]) == 2
