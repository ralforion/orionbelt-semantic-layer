"""The bundled starter model must run, not merely compile.

The previous starter (``examples/sem-layer.obml.yml``) declared column codes
no seeded database had: ``sales_client`` where every seed has ``salesclient``.
It compiled cleanly, so the whole test suite was happy, and it failed at
execution with ``column Sales.sales_client does not exist`` the moment anyone
pressed Run in the UI. Nothing caught it because everything that consumed it
compiled, validated or described it, and nothing ever executed it.

These execute against the real DuckDB seed. They are the reason it is safe to
ship one model for both teaching and running.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb required to execute the seed")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.models.query import QueryObject  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_MODEL = _REPO / "examples" / "orionbelt_1_commerce.yaml"
_SEED = _REPO / "examples" / "orionbelt_1_commerce.duckdb"
_BUILDER = _REPO / "scripts" / "build_demo_duckdb.py"


@pytest.fixture(scope="module")
def seed() -> Path:
    """The demo DuckDB file, built if absent (it is gitignored)."""
    if not _SEED.exists():
        subprocess.run([sys.executable, str(_BUILDER)], check=True, capture_output=True)
    return _SEED


@pytest.fixture(scope="module")
def model():
    raw, src = TrackedLoader().load(_MODEL)
    resolved, result = ReferenceResolver().resolve(raw, src)
    assert resolved is not None, result.errors
    assert not result.errors, result.errors
    return resolved


def _execute(model, seed: Path, query: dict) -> list:
    sql = CompilationPipeline().compile(QueryObject(**query), model, "duckdb").sql
    con = duckdb.connect(str(seed), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_a_plain_query_executes(model, seed: Path) -> None:
    """The shape that used to fail: real column codes against a real table."""
    rows = _execute(
        model,
        seed,
        {
            "select": {"dimensions": ["Country Name"], "measures": ["Total Sales"]},
            "limit": 5,
        },
    )
    assert rows, "the starter model returned no rows against its own seed"


def test_the_named_secondary_path_changes_the_answer(model, seed: Path) -> None:
    """Naming the path re-reads Country Name as the supplier's, not the client's.

    Asserting the results *differ* rather than matching fixed values: the point
    of a named path is that it answers a different question, and a port that
    silently left it a no-op would still return rows.
    """
    query = {
        "select": {
            "dimensions": ["Supplier Name", "Country Name"],
            "measures": ["Avg Unit Price"],
        },
    }
    via_client = _execute(model, seed, query)
    via_supplier = _execute(
        model,
        seed,
        {
            **query,
            "usePathNames": [
                {"source": "Suppliers", "target": "Countries", "pathName": "supplier_country"}
            ],
        },
    )

    assert via_client and via_supplier
    assert via_client != via_supplier, "the secondary path made no difference"


def test_filter_context_ignores_the_query_filter(model, seed: Path) -> None:
    """Unfiltered Sales reads one grand total whatever the WHERE says."""
    rows = _execute(
        model,
        seed,
        {
            "select": {
                "dimensions": ["Country Name"],
                "measures": ["Total Sales", "Unfiltered Sales"],
            },
            "where": [{"field": "Country Name", "op": "in", "value": ["Germany", "France"]}],
        },
    )

    assert len(rows) == 2
    totals = {r[2] for r in rows}
    assert len(totals) == 1, "the filter-context measure varied with the query filter"
    filtered = {r[1] for r in rows}
    assert len(filtered) == 2, "the ordinary measure should differ per country"
    assert max(totals) > max(filtered), "the unfiltered total should exceed either country"


def test_the_grain_override_holds_the_country_total(model, seed: Path) -> None:
    """Sales by Country stays at country grain while the query groups finer."""
    rows = _execute(
        model,
        seed,
        {
            "select": {
                "dimensions": ["Country Name", "Product Category"],
                "measures": ["Total Sales", "Sales by Country"],
            },
        },
    )

    assert rows
    by_country: dict[str, set] = {}
    for country, _category, _total, fixed in rows:
        by_country.setdefault(country, set()).add(fixed)
    assert all(len(v) == 1 for v in by_country.values()), (
        "the fixed-grain measure varied across categories within one country"
    )


def test_the_measure_level_filter_narrows_only_itself(model, seed: Path) -> None:
    """Electronics Sales carries its own predicate; Total Sales stays whole."""
    rows = _execute(
        model,
        seed,
        {
            "select": {
                "dimensions": ["Country Name"],
                "measures": ["Total Sales", "Electronics Sales"],
            },
        },
    )

    assert rows
    assert all(electronics is None or electronics <= total for _c, total, electronics in rows), (
        "a category-filtered measure cannot exceed the unfiltered total"
    )
    assert any(
        electronics is not None and electronics < total for _c, total, electronics in rows
    ), "the filter had no effect anywhere"


def test_the_distinct_count_is_not_the_row_count(model, seed: Path) -> None:
    rows = _execute(
        model,
        seed,
        {
            "select": {
                "dimensions": ["Country Name"],
                "measures": ["Distinct Clients", "Sales Count"],
            },
        },
    )

    assert rows
    assert all(distinct <= sales for _c, distinct, sales in rows)
    assert any(distinct < sales for _c, distinct, sales in rows), "distinct never collapsed"
