"""Execute every measure and metric of the commerce model against live Dremio.

For each measure and metric, run a query through the OBSL pgwire surface
directly (``localhost:15432``), which compiles to Dremio SQL and executes it
back against Dremio over Arrow Flight. This is the layer that a compile-only
or DuckDB test cannot cover: only real Dremio execution catches dialect SQL
that parses but the engine rejects.

This suite was added after two such bugs shipped undetected -- quarter-grain
period-over-period emitting an invalid ``INTERVAL '-1' QUARTER``, and a
``previousValue`` projection that Dremio miscompiled -- because nothing ever
executed the compiled Dremio SQL for the metric surface.

Opt-in via the ``dremio`` marker and run through the dedicated test stack::

    tests/integration/dremio/run.sh

which builds the stack, exports ``OBSL_MODEL_NAME=orionbelt_1_commerce`` (the
model parametrised here), runs ``pytest -m dremio``, and tears down. Skips
cleanly if psycopg is missing, pgwire is unreachable, or the served model
does not expose the commerce measures.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.integration.dremio.conftest import OBSL_MODEL_NAME

pytestmark = pytest.mark.dremio

OBSL_PGWIRE_HOST_LOCAL = os.environ.get("OBSL_PGWIRE_HOST_LOCAL", "localhost")
OBSL_PGWIRE_PORT_LOCAL = int(os.environ.get("OBSL_PGWIRE_PORT_LOCAL", "15432"))

# The demo stack serves the commerce model; its measure/metric surface matches
# the committed source model, which we read to parametrise the sweep.
REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_MODEL_YAML = REPO_ROOT / "examples" / "orionbelt_1_commerce.yaml"


def _load_model() -> dict[str, Any]:
    if not SOURCE_MODEL_YAML.exists():
        return {}
    with SOURCE_MODEL_YAML.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


_MODEL = _load_model()
_MEASURES = list((_MODEL.get("measures") or {}).keys())
_METRICS = _MODEL.get("metrics") or {}


def _time_dimension(spec: dict[str, Any]) -> str | None:
    """The dimension a cumulative / period-over-period metric requires in SELECT."""
    return spec.get("timeDimension") or (spec.get("periodOverPeriod") or {}).get("timeDimension")


def _sql(item: str, dims: list[str]) -> str:
    cols = ", ".join(f'"{c}"' for c in [*dims, item])
    order = f" ORDER BY {', '.join(chr(34) + d + chr(34) for d in dims)}" if dims else ""
    return f"SELECT {cols} FROM model{order} LIMIT 50"


@pytest.fixture(scope="module")
def pgwire_cursor():  # type: ignore[no-untyped-def]
    """A cursor on a direct OBSL pgwire connection, or skip if unavailable."""
    psycopg = pytest.importorskip("psycopg", reason="psycopg required for the pgwire sweep")
    if not _MEASURES:
        pytest.skip(f"source model not found at {SOURCE_MODEL_YAML}")
    try:
        conn = psycopg.connect(
            host=OBSL_PGWIRE_HOST_LOCAL,
            port=OBSL_PGWIRE_PORT_LOCAL,
            user="obsl",
            password="trust",
            dbname=OBSL_MODEL_NAME,
            sslmode="disable",
            connect_timeout=10,
        )
    except Exception:  # noqa: BLE001 -- no stack up -> skip, don't fail
        endpoint = f"{OBSL_PGWIRE_HOST_LOCAL}:{OBSL_PGWIRE_PORT_LOCAL}"
        pytest.skip(f"OBSL pgwire not reachable at {endpoint}")
    with conn:
        cur = conn.cursor()
        # Probe the served model before parametrised cases run.
        try:
            cur.execute(_sql(_MEASURES[0], []))
            cur.fetchall()
        except Exception:  # noqa: BLE001
            conn.rollback()
            pytest.skip(f"stack on pgwire does not serve the commerce model as {OBSL_MODEL_NAME!r}")
        yield cur


@pytest.mark.parametrize("measure", _MEASURES)
def test_measure_executes_on_dremio(pgwire_cursor, measure: str) -> None:  # type: ignore[no-untyped-def]
    """Every measure must execute (grand total) against live Dremio."""
    pgwire_cursor.execute(_sql(measure, []))
    pgwire_cursor.fetchall()  # raises on a Dremio execution error


@pytest.mark.parametrize("metric", list(_METRICS.keys()))
def test_metric_executes_on_dremio(pgwire_cursor, metric: str) -> None:  # type: ignore[no-untyped-def]
    """Every metric must execute against live Dremio, with its required dimension.

    Cumulative / period-over-period metrics need their time dimension in the
    projection; derived metrics are grouped by a plain dimension.
    """
    time_dim = _time_dimension(_METRICS[metric])
    dims = [time_dim] if time_dim else ["Product Category"]
    pgwire_cursor.execute(_sql(metric, dims))
    pgwire_cursor.fetchall()  # raises on a Dremio execution error
