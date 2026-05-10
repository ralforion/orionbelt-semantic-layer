"""Vendor-execution sweep — every corpus query × every available vendor.

For each ``(corpus_entry, vendor)`` pair this test:

1. compiles the corpus query through the OBSL pipeline for the
   vendor's dialect;
2. executes the resulting SQL against a freshly-seeded testcontainer
   (or in-memory DuckDB);
3. normalises the row set (string-coerced values, canonical sort);
4. compares it byte-for-byte to the DuckDB golden snapshot at
   ``tests/integration/drift/duckdb/<id>.yaml``.

The DuckDB golden is the cross-vendor reference: if Postgres,
MySQL, ClickHouse, *or* DuckDB-via-our-seed-loader produces a
different row set, the test fails loudly with a per-row diff. This
catches dialect-specific binder bugs, type-promotion drift, and
seed-loader regressions in one place.

Gated by the ``docker`` pytest marker — `pytest -m docker
tests/integration/drift/vendor_exec/`.
"""

from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pytest
import yaml

# Re-use the existing CORPUS loader from the correctness package.
from tests.integration.correctness._corpus import CORPUS, CorpusEntry  # noqa: E402

from .conftest import VendorTarget

duckdb = pytest.importorskip("duckdb", reason="duckdb required for vendor_exec")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[4]
COMMERCE_MODEL_YAML = REPO_ROOT / "examples" / "orionbelt_1_commerce.yaml"
GOLDEN_DIR = Path(__file__).resolve().parents[1] / "duckdb"

# Canonical decimal serialisation: optional sign, integer with no leading
# zeros (or a single ``0``), optional fractional part. Excludes scientific
# notation (``1e3``) and zero-padded IDs (``00123``) so they stay distinct
# string values during cross-vendor row comparison.
_CANONICAL_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")


# Mark every test in this file as docker-gated.
pytestmark = pytest.mark.docker


@pytest.fixture(scope="session")
def commerce_model() -> SemanticModel:
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, source_map = loader.load(COMMERCE_MODEL_YAML)
    model, result = resolver.resolve(raw, source_map)
    assert result.valid, f"Commerce model has validation errors: {result.errors}"
    return model


@pytest.fixture(scope="session")
def pipeline() -> CompilationPipeline:
    return CompilationPipeline()


# ---------------------------------------------------------------------------
# Row normalisation — must match the harness used to produce the
# golden snapshots so the comparison is meaningful. See
# ``tests/integration/drift/conftest.py::_normalize_rows``.
# ---------------------------------------------------------------------------


def _normalize_value(v: Any) -> Any:
    if v is None:
        return None
    # Mirror ``drift/conftest.py``: Decimal → str (preserves precision),
    # date / datetime → ISO string, scalars pass through. Drop tzinfo
    # before isoformat — Postgres returns ``DATE_TRUNC`` results as
    # ``timestamp with time zone`` (``+00:00`` suffix), DuckDB returns
    # naive timestamps; both represent the same instant for our seed
    # so we collapse to the naive form for comparison.
    if isinstance(v, _dt.datetime):
        if v.tzinfo is not None:
            v = v.replace(tzinfo=None)
        s = v.isoformat()
        # Collapse midnight timestamps to plain date — MySQL's DATE_TRUNC
        # equivalent returns ``DATE``, DuckDB returns ``TIMESTAMP``. Both
        # represent the same calendar day for our seed.
        return s[:10] if s.endswith("T00:00:00") else s
    if isinstance(v, _dt.date):
        return v.isoformat()
    if isinstance(v, str) and len(v) == 19 and v.endswith("T00:00:00"):
        # Same collapse for goldens that came in as ISO strings.
        return v[:10]

    # Round numeric values to 11 significant digits *via float*. Going
    # through ``float`` canonicalises trailing zeros that ``Decimal``
    # preserves but floats drop, so cross-engine output (Postgres
    # returns ``Decimal('-0.02233628881800')``, DuckDB the float
    # ``-0.022336288817980665``) compares equal. 11 sig figs preserves
    # money sums up to ~$100 B at 2-dp precision while absorbing the
    # last-bit ULP drift between Decimal arithmetic (ClickHouse) and
    # float arithmetic (DuckDB / Postgres) that surfaces in YoY-style
    # ratios at the 12th significant digit.
    #
    # Goldens store Decimals as strings (``"100.50"``); to keep the
    # comparison symmetric we run numeric-looking strings through the
    # same pipeline. Country names / ISO dates / NULLs don't parse as
    # Decimal so they fall through unchanged.
    #
    # Restrict the string→numeric coercion to the *canonical* decimal
    # form Python's ``Decimal.__str__`` produces (no leading zeros,
    # no scientific notation). That keeps zero-padded keys like
    # ``"00123"`` and exponent-form strings like ``"1e3"`` as distinct
    # string values — coercing them would silently collapse different
    # IDs into equal cells and mask cross-vendor key-handling bugs.
    if isinstance(v, (Decimal, float)):
        return f"{float(v):.11g}"
    if isinstance(v, str) and _CANONICAL_DECIMAL_RE.match(v):
        try:
            return f"{float(Decimal(v)):.11g}"
        except (InvalidOperation, ValueError):
            return v
    return v


def _normalize_rows(rows: list[dict[str, Any]]) -> list[list[Any]]:
    if not rows:
        return []
    cols = list(rows[0].keys())
    ordered = [[_normalize_value(r[c]) for c in cols] for r in rows]
    ordered.sort(key=lambda row: [("" if x is None else str(x)) for x in row])
    return ordered


def _load_golden(query_id: str) -> list[list[Any]]:
    """Load + re-normalise the DuckDB golden so it can be compared cell-by-cell.

    The golden YAML was written by the regular drift harness, which
    leaves Python floats as-is (e.g. ``0.9931620307032472``). The
    vendor-exec normalisation rounds floats to 10 significant digits to
    absorb cross-engine ULP drift; we apply the same pass to the golden
    so the two sides are on equal footing.
    """
    path = GOLDEN_DIR / f"{query_id}.yaml"
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    raw_rows = data.get("rows") or []
    return [[_normalize_value(c) for c in row] for row in raw_rows]


# ---------------------------------------------------------------------------
# Per-vendor parametrize. We split into one test per vendor so that a
# missing container library only skips its own tests, not all four.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Known cross-vendor differences. Each entry is a (vendor, query_id)
# pair plus a one-line reason describing whether it's a real OBSL bug
# or an acceptable engine-rounding variance. Marked ``xfail`` so the
# test is tracked but doesn't break CI.
# ---------------------------------------------------------------------------

_KNOWN_ISSUES: dict[tuple[str, str], str] = {}


def _assert_matches_golden(
    entry: CorpusEntry,
    vendor: VendorTarget,
    commerce_model: SemanticModel,
    pipeline: CompilationPipeline,
) -> None:
    if (vendor.name, entry.id) in _KNOWN_ISSUES:
        pytest.xfail(_KNOWN_ISSUES[(vendor.name, entry.id)])
    sql = pipeline.compile(entry.query, commerce_model, vendor.dialect).sql
    rows = vendor.execute(sql)
    actual = _normalize_rows(rows)

    # Goldens are capped at 200 rows in the original DuckDB drift test
    # for queries that produce many rows — apply the same cap here so
    # comparisons line up.
    if len(actual) > 200:
        actual = actual[:200]

    expected = _load_golden(entry.id)
    if not expected:
        pytest.skip(f"No golden snapshot at drift/duckdb/{entry.id}.yaml")

    pairs = zip(actual, expected, strict=False)
    first_diff = next((i for i, (a, b) in enumerate(pairs) if a != b), "N/A")
    assert actual == expected, (
        f"Vendor {vendor.name} produced different rows than the DuckDB golden "
        f"for corpus {entry.id!r}.\n"
        f"  golden rows: {len(expected)}\n"
        f"  vendor rows: {len(actual)}\n"
        f"  first diverging index: {first_diff}\n"
        f"  vendor sample: {actual[:3]}\n"
        f"  golden sample: {expected[:3]}"
    )


@pytest.mark.parametrize("entry", CORPUS, ids=lambda e: e.id)
def test_duckdb_vendor_exec(
    entry: CorpusEntry,
    vendor_duckdb: VendorTarget,
    commerce_model: SemanticModel,
    pipeline: CompilationPipeline,
) -> None:
    """In-memory DuckDB seeded via the shared loader — control vendor."""
    _assert_matches_golden(entry, vendor_duckdb, commerce_model, pipeline)


@pytest.mark.parametrize("entry", CORPUS, ids=lambda e: e.id)
def test_postgres_vendor_exec(
    entry: CorpusEntry,
    vendor_postgres: VendorTarget,
    commerce_model: SemanticModel,
    pipeline: CompilationPipeline,
) -> None:
    """Postgres 16 testcontainer."""
    _assert_matches_golden(entry, vendor_postgres, commerce_model, pipeline)


@pytest.mark.parametrize("entry", CORPUS, ids=lambda e: e.id)
def test_mysql_vendor_exec(
    entry: CorpusEntry,
    vendor_mysql: VendorTarget,
    commerce_model: SemanticModel,
    pipeline: CompilationPipeline,
) -> None:
    """MySQL 8 testcontainer."""
    _assert_matches_golden(entry, vendor_mysql, commerce_model, pipeline)


@pytest.mark.parametrize("entry", CORPUS, ids=lambda e: e.id)
def test_clickhouse_vendor_exec(
    entry: CorpusEntry,
    vendor_clickhouse: VendorTarget,
    commerce_model: SemanticModel,
    pipeline: CompilationPipeline,
) -> None:
    """ClickHouse latest testcontainer."""
    _assert_matches_golden(entry, vendor_clickhouse, commerce_model, pipeline)
