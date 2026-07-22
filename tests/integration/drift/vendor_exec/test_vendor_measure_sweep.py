"""Execute every measure and metric against every local vendor engine.

The corpus-based ``test_vendor_exec`` checks 15 curated queries for
byte-for-byte agreement with the DuckDB golden. This sweep is the breadth
complement: it runs *every* measure and *every* metric of the commerce
model against each vendor's testcontainer and asserts it executes at all.

That breadth is what catches dialect SQL which compiles cleanly but the
engine rejects at runtime -- the class of bug that shipped undetected
because no test executed the full metric surface per vendor (a quarter
period-over-period ``INTERVAL '-1' QUARTER`` on Dremio, a ``ValueError``
on Databricks/ClickHouse date arithmetic, a Dremio ``previousValue``
miscompile). Correctness of the numbers stays the corpus test's job; this
one asserts execution only.

Gated by the ``docker`` marker (Postgres/MySQL/ClickHouse testcontainers;
DuckDB in-memory)::

    uv run pytest -m docker tests/integration/drift/vendor_exec/test_vendor_measure_sweep.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

REPO_ROOT = Path(__file__).resolve().parents[4]
COMMERCE_MODEL_YAML = REPO_ROOT / "examples" / "orionbelt_1_commerce.yaml"

_RAW = yaml.safe_load(COMMERCE_MODEL_YAML.read_text()) if COMMERCE_MODEL_YAML.exists() else {}
_METRICS = _RAW.get("metrics") or {}


def _measure_names() -> list[str]:
    """Full queryable measure namespace via ``effective_measures``.

    Includes synthesised row-count measures (``Sales Count`` etc.), not just
    declared measures. Falls back to the declared list at collection time.
    """
    if not COMMERCE_MODEL_YAML.exists():
        return []
    try:
        raw, source_map = TrackedLoader().load(COMMERCE_MODEL_YAML)
        model, result = ReferenceResolver().resolve(raw, source_map)
        if result.valid:
            return list(model.effective_measures.keys())
    except Exception:  # noqa: BLE001 -- fall back to declared measures at collection time
        pass
    return list((_RAW.get("measures") or {}).keys())


_MEASURES = _measure_names()


def _time_dimension(spec: dict[str, Any]) -> str | None:
    return spec.get("timeDimension") or (spec.get("periodOverPeriod") or {}).get("timeDimension")


# (kind, name, dimensions) — measures as grand totals; cumulative / PoP
# metrics with their required time dimension; derived metrics grouped by a
# plain dimension.
def _sweep_items() -> list[tuple[str, str, list[str]]]:
    items: list[tuple[str, str, list[str]]] = [("measure", m, []) for m in _MEASURES]
    for name, spec in _METRICS.items():
        time_dim = _time_dimension(spec)
        items.append(("metric", name, [time_dim] if time_dim else ["Product Category"]))
    return items


_SWEEP = _sweep_items()
_IDS = [f"{kind}:{name}" for kind, name, _ in _SWEEP]


@pytest.fixture(scope="session")
def commerce_model() -> SemanticModel:
    raw, source_map = TrackedLoader().load(COMMERCE_MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, f"Commerce model has validation errors: {result.errors}"
    return model


@pytest.fixture(scope="session")
def pipeline() -> CompilationPipeline:
    return CompilationPipeline()


def _query(name: str, dims: list[str]) -> QueryObject:
    return QueryObject.model_validate({"select": {"dimensions": dims, "measures": [name]}})


def _exec_item(
    kind: str,
    name: str,
    dims: list[str],
    vendor: VendorTarget,
    model: SemanticModel,
    pipeline: CompilationPipeline,
) -> None:
    sql = pipeline.compile(_query(name, dims), model, vendor.dialect).sql
    rows = vendor.execute(sql)  # raises on a vendor execution error
    assert isinstance(rows, list), f"{vendor.name}: {kind} {name!r} did not return rows"


@pytest.mark.parametrize("kind,name,dims", _SWEEP, ids=_IDS)
def test_duckdb_measure_sweep(
    kind: str,
    name: str,
    dims: list[str],
    vendor_duckdb: VendorTarget,
    commerce_model: SemanticModel,
    pipeline: CompilationPipeline,
) -> None:
    _exec_item(kind, name, dims, vendor_duckdb, commerce_model, pipeline)


@pytest.mark.parametrize("kind,name,dims", _SWEEP, ids=_IDS)
def test_postgres_measure_sweep(
    kind: str,
    name: str,
    dims: list[str],
    vendor_postgres: VendorTarget,
    commerce_model: SemanticModel,
    pipeline: CompilationPipeline,
) -> None:
    _exec_item(kind, name, dims, vendor_postgres, commerce_model, pipeline)


@pytest.mark.parametrize("kind,name,dims", _SWEEP, ids=_IDS)
def test_mysql_measure_sweep(
    kind: str,
    name: str,
    dims: list[str],
    vendor_mysql: VendorTarget,
    commerce_model: SemanticModel,
    pipeline: CompilationPipeline,
) -> None:
    _exec_item(kind, name, dims, vendor_mysql, commerce_model, pipeline)


@pytest.mark.parametrize("kind,name,dims", _SWEEP, ids=_IDS)
def test_clickhouse_measure_sweep(
    kind: str,
    name: str,
    dims: list[str],
    vendor_clickhouse: VendorTarget,
    commerce_model: SemanticModel,
    pipeline: CompilationPipeline,
) -> None:
    _exec_item(kind, name, dims, vendor_clickhouse, commerce_model, pipeline)
