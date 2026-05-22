"""Stage 2: the full circle.

Dremio → OBSL pgwire → (OBSL compiles to Dremio dialect) → ob_dremio
Flight client → Dremio executes against its own INFORMATION_SCHEMA →
results stream back through OBSL to Dremio's federation engine to the
test client.

Backing data is Dremio's built-in ``INFORMATION_SCHEMA.COLUMNS`` — it's
always present (Dremio's own system catalogs) so no dataset promotion
or sample-data setup is needed.

Two OBSL models live in the test stack at once:

* ``orionbelt_1_commerce`` — Stage 1, DuckDB backend
* ``dremio_info_schema``  — Stage 2, Dremio backend
  (``settings.defaultDialect: dremio``)

Both are exposed through the same OBSL pgwire port, and the per-model
dialect picker in the router routes each one to the right executor.
"""

from __future__ import annotations

import httpx
import pytest

from tests.integration.dremio.conftest import (
    OBSL_STAGE2_MODEL_NAME,
    DremioSession,
    RunSql,
)

pytestmark = pytest.mark.dremio

STAGE2_SOURCE_NAME = "obsl_pg_dremio"


@pytest.fixture(scope="session")
def stage2_source(dremio_session: DremioSession) -> str:
    """Register a second Postgres source in Dremio targeting the Dremio-backed model.

    Distinct from the Stage-1 ``obsl_pg`` source because Dremio's
    Postgres source carries the ``databaseName`` (= OBSL pgwire database
    parameter = model name) in its config; we need one source per model.
    """

    with httpx.Client(base_url=dremio_session.base_url) as client:
        existing = client.get(
            f"/api/v3/catalog/by-path/{STAGE2_SOURCE_NAME}",
            headers=dremio_session.headers(),
            timeout=10.0,
        )
        if existing.status_code == 200:
            return STAGE2_SOURCE_NAME

        body = {
            "entityType": "source",
            "name": STAGE2_SOURCE_NAME,
            "type": "POSTGRES",
            "config": {
                "hostname": "obsl",
                "port": "5432",
                "databaseName": OBSL_STAGE2_MODEL_NAME,
                "authenticationType": "MASTER",
                "username": "obsl",
                "password": "obsl",
                "useSsl": False,
                "fetchSize": 200,
            },
            "metadataPolicy": {
                "authTTLMs": 86_400_000,
                "namesRefreshMs": 3_600_000,
                "datasetRefreshAfterMs": 3_600_000,
                "datasetExpireAfterMs": 10_800_000,
                "datasetUpdateMode": "PREFETCH_QUERIED",
            },
        }
        created = client.post(
            "/api/v3/catalog",
            json=body,
            headers=dremio_session.headers(),
            timeout=30.0,
        )
        if created.status_code not in (200, 201):
            raise RuntimeError(
                f"Failed to register Stage-2 source in Dremio: "
                f"{created.status_code} {created.text!r}"
            )
        return STAGE2_SOURCE_NAME


def test_stage2_source_registers(stage2_source: str) -> None:
    """Conftest creates the second Postgres source — landing here proves it."""

    assert stage2_source == STAGE2_SOURCE_NAME


def test_full_circle_dim_measure(run_dremio_sql: RunSql, stage2_source: str) -> None:
    """End-to-end: Dremio → OBSL → Dremio dialect → Dremio Flight → INFORMATION_SCHEMA.

    Asserts a non-empty result with the expected column shape. The actual
    row count varies with Dremio's internal catalog size but is always > 0
    because Dremio's own ``sys`` / ``INFORMATION_SCHEMA`` always have rows.
    """

    rows = run_dremio_sql(
        'SELECT "Table Schema", "Column Count" '
        f'FROM {stage2_source}."{OBSL_STAGE2_MODEL_NAME}".model '
        'ORDER BY "Column Count" DESC '
        "LIMIT 5"
    )
    assert rows, "Stage-2 full-circle query returned no rows"
    assert len(rows[0]) == 2, f"expected 2 cols, got {len(rows[0])}"
    # Column Count is an aggregate; every row must be a positive integer.
    for row in rows:
        count = row[1]
        assert isinstance(count, (int, float, str)), f"unexpected type {type(count)!r}"
        assert int(count) > 0, f"non-positive Column Count in row {row!r}"
