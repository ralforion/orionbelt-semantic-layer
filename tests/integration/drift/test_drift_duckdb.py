"""Tier 2 drift — DuckDB execution snapshots.

For every entry in the v0 corpus, capture the compiled SQL and the
canonical-sorted result rows under ``drift/duckdb/<id>.yaml``. The
corpus itself lives under ``tests/integration/correctness/`` (manifest
+ pure-OBML query files) and is shared with the Tier 1 hand-SQL test.

Re-snap with::

    UPDATE_SNAPSHOTS=1 uv run pytest tests/integration/drift/test_drift_duckdb.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb required for drift execution snapshots")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402

from ..correctness._corpus import CORPUS, CorpusEntry  # noqa: E402
from .conftest import assert_exec_snapshot  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMERCE_MODEL_YAML = REPO_ROOT / "examples" / "orionbelt_1_commerce.yaml"
COMMERCE_DUCKDB = REPO_ROOT / "examples" / "orionbelt_1_commerce.duckdb"


def _require_seed() -> None:
    if not COMMERCE_DUCKDB.exists() or not COMMERCE_MODEL_YAML.exists():
        pytest.skip(
            "Bundled DuckDB seed or commerce YAML not found. "
            "Run scripts/build_demo_duckdb.py to generate them."
        )


@pytest.fixture(scope="module")
def commerce_model() -> SemanticModel:
    _require_seed()
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, source_map = loader.load(COMMERCE_MODEL_YAML)
    model, result = resolver.resolve(raw, source_map)
    assert result.valid, f"Commerce model has validation errors: {result.errors}"
    return model


@pytest.fixture(scope="module")
def commerce_db() -> duckdb.DuckDBPyConnection:
    _require_seed()
    conn = duckdb.connect(database=str(COMMERCE_DUCKDB), read_only=True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="module")
def pipeline() -> CompilationPipeline:
    return CompilationPipeline()


def _execute(conn: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


@pytest.mark.parametrize("entry", CORPUS, ids=lambda e: e.id)
def test_drift_duckdb_exec(
    entry: CorpusEntry,
    commerce_db: duckdb.DuckDBPyConnection,
    commerce_model: SemanticModel,
    pipeline: CompilationPipeline,
) -> None:
    """Compile + execute each corpus query against DuckDB; diff vs. snapshot."""
    sql = pipeline.compile(entry.query, commerce_model, "duckdb").sql
    rows = _execute(commerce_db, sql)
    # Cap rows for queries that produce many rows. Canonical-sort in the
    # harness keeps the slice deterministic.
    if len(rows) > 200:

        def _sortkey(r: dict[str, Any]) -> tuple[str, ...]:
            return tuple(("" if v is None else str(v)) for v in r.values())

        rows = sorted(rows, key=_sortkey)[:200]

    assert_exec_snapshot(
        entry.id,
        sql=sql,
        rows=rows,
        last_verified_by=entry.last_verified_by,
    )
