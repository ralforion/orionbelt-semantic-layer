"""Fixtures for the Tier 1 correctness suite.

Tier 1 ratifies query results via *independent* computation paths (see
``design/PLAN_correctness_and_drift_tests.md``). All tests in this package
run against the bundled DuckDB seed (``examples/orionbelt_1_commerce.duckdb``)
and the matching OBML model (``examples/orionbelt_1_commerce.yaml``).

The ``run_query`` fixture is the single entry point used by every Tier 1
test: it compiles a ``QueryObject`` through the full OBSL pipeline, executes
the SQL against the seed, and returns rows as ``list[dict]`` with ``Decimal``
values preserved (no float casting). Per the plan §10.1, sums must be
asserted via exact ``Decimal`` equality; only ratios/averages may use
``pytest.approx``.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb required for correctness tests")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.models.query import QueryObject  # noqa: E402
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMERCE_MODEL_YAML = REPO_ROOT / "examples" / "orionbelt_1_commerce.yaml"
COMMERCE_DUCKDB = REPO_ROOT / "examples" / "orionbelt_1_commerce.duckdb"


def _require_seed() -> None:
    if not COMMERCE_DUCKDB.exists():
        pytest.skip(
            f"Bundled DuckDB seed not found at {COMMERCE_DUCKDB}. "
            "Run scripts/build_demo_duckdb.py to generate it."
        )
    if not COMMERCE_MODEL_YAML.exists():
        pytest.skip(f"Commerce model YAML not found at {COMMERCE_MODEL_YAML}.")


@pytest.fixture(scope="session")
def commerce_model() -> SemanticModel:
    _require_seed()
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, source_map = loader.load(COMMERCE_MODEL_YAML)
    model, result = resolver.resolve(raw, source_map)
    assert result.valid, f"Commerce model has validation errors: {result.errors}"
    return model


@pytest.fixture(scope="session")
def commerce_db() -> duckdb.DuckDBPyConnection:
    _require_seed()
    conn = duckdb.connect(database=str(COMMERCE_DUCKDB), read_only=True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="session")
def pipeline() -> CompilationPipeline:
    return CompilationPipeline()


def _rows_as_dicts(conn: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    """Execute ``sql`` on ``conn`` and return rows as dicts.

    ``Decimal`` values are kept as ``Decimal`` so callers can assert exact
    equality on sums (see plan §10.1).
    """
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


@pytest.fixture(scope="session")
def run_query(
    commerce_db: duckdb.DuckDBPyConnection,
    commerce_model: SemanticModel,
    pipeline: CompilationPipeline,
) -> Callable[[QueryObject], list[dict[str, Any]]]:
    """Return a callable that compiles + executes a ``QueryObject``.

    Usage::

        rows = run_query(QueryObject(select=QuerySelect(measures=["Total Sales"])))
        total = rows[0]["Total Sales"]  # Decimal
    """

    def _run(query: QueryObject, *, dialect: str = "duckdb") -> list[dict[str, Any]]:
        sql = pipeline.compile(query, commerce_model, dialect).sql
        return _rows_as_dicts(commerce_db, sql)

    return _run


def assert_decimal_equal(
    a: Decimal | int | float,
    b: Decimal | int | float,
    *,
    msg: str = "",
) -> None:
    """Strict equality for sums.

    Both operands are coerced to ``Decimal`` (NOT float) before comparison,
    so this is the right helper for additive measures where the plan
    forbids float tolerance.
    """
    da = a if isinstance(a, Decimal) else Decimal(str(a))
    db = b if isinstance(b, Decimal) else Decimal(str(b))
    assert da == db, msg or f"Decimal mismatch: {da} != {db}"
