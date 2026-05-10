"""Tier 2 drift — per-dialect compile-only SQL snapshots.

For every (corpus query, registered dialect) pair, capture the compiled
SQL string under ``drift/compile_only/<dialect>/<id>.sql``. Catches
dialect-specific emit drift (e.g. DECIMAL → NUMBER, BOOLEAN
representation, LISTAGG vs STRING_AGG) without needing a live database
for any vendor other than DuckDB.

Re-snap with::

    UPDATE_SNAPSHOTS=1 uv run pytest tests/integration/drift/test_drift_compile_only.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

import orionbelt.dialect  # noqa: F401  -- triggers dialect registrations
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from ..correctness._corpus import CORPUS, CorpusEntry
from .conftest import assert_compile_only_snapshot

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMERCE_MODEL_YAML = REPO_ROOT / "examples" / "orionbelt_1_commerce.yaml"

DIALECTS = sorted(DialectRegistry.available())


@pytest.fixture(scope="module")
def commerce_model() -> SemanticModel:
    if not COMMERCE_MODEL_YAML.exists():
        pytest.skip(f"Commerce model YAML not found at {COMMERCE_MODEL_YAML}.")
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, source_map = loader.load(COMMERCE_MODEL_YAML)
    model, result = resolver.resolve(raw, source_map)
    assert result.valid, f"Commerce model has validation errors: {result.errors}"
    return model


@pytest.fixture(scope="module")
def pipeline() -> CompilationPipeline:
    return CompilationPipeline()


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("entry", CORPUS, ids=lambda e: e.id)
def test_drift_compile_only(
    entry: CorpusEntry,
    dialect: str,
    commerce_model: SemanticModel,
    pipeline: CompilationPipeline,
) -> None:
    """Compile each corpus query for each registered dialect; diff vs snapshot."""
    try:
        sql = pipeline.compile(entry.query, commerce_model, dialect).sql
    except Exception as exc:
        # Some queries may not be supported by every dialect. Surface the
        # failure as a snapshot of the error message so future fixes show
        # up as legitimate drift rather than silent skips.
        sql = f"-- COMPILE ERROR: {type(exc).__name__}: {exc}\n"
    assert_compile_only_snapshot(entry.id, dialect=dialect, sql=sql)
