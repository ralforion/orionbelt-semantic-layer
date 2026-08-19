"""The FinOps demo's nested columns, executed against the generated database.

The showcase exists to demonstrate something a flattened model cannot do, and
the claims it makes are arithmetic ones: label keys are data, a parent measure
grouped by a nested dimension does not double count, and a nested measure does
not lose a duplicate. A page saying so is worth nothing if the model stops
answering that way, and every number in
``docs/examples/finops-focus.md`` comes from here.

The database is gitignored, so it is **built when absent** rather than skipped
around - the same thing ``test_starter_model_executes`` does with its seed, and
for the same reason: a test that quietly skips in CI protects nothing, and these
numbers are published. The generator takes about a second.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb required for the FinOps showcase")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.models.query import QueryObject, QuerySelect  # noqa: E402
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser import ReferenceResolver, TrackedLoader  # noqa: E402
from orionbelt.parser.validator import SemanticValidator  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
_DB = _ROOT / "examples" / "finops.duckdb"
_MODEL = _ROOT / "examples" / "finops.obml.yml"
_BUILDER = _ROOT / "scripts" / "build_finops_duckdb.py"


@pytest.fixture(scope="module")
def db() -> Path:
    """The demo database, built if absent (it is gitignored)."""
    if not _DB.exists():
        subprocess.run([sys.executable, str(_BUILDER)], check=True, capture_output=True)
    return _DB


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    resolved, result = ReferenceResolver().resolve(*TrackedLoader().load_string(_MODEL.read_text()))
    assert result.valid, result.errors
    assert not SemanticValidator().validate(resolved)
    return resolved


@pytest.fixture(scope="module")
def con(db: Path) -> Any:
    connection = duckdb.connect(str(db), read_only=True)
    yield connection
    connection.close()


def _run(con: Any, model: SemanticModel, dimensions: list[str], measures: list[str]) -> dict:
    """Compile and execute, returning ``{dimension value: measures}``."""
    result = CompilationPipeline().compile(
        QueryObject(select=QuerySelect(dimensions=dimensions, measures=measures)), model, "duckdb"
    )
    rows = con.execute(result.sql).fetchall()
    return {r[0]: tuple(float(v) if v is not None else None for v in r[1:]) for r in rows}


def test_the_charge_declares_a_key(model: SemanticModel, con: Any) -> None:
    """Everything below rests on it, and FOCUS does not supply one."""
    charges = model.data_objects["Charges"]
    keys = [name for name, col in charges.columns.items() if col.primary_key]
    assert keys == ["Charge Key"]
    total, distinct = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT ChargeKey) FROM focus.charges"
    ).fetchone()
    assert total == distinct, "ChargeKey must identify one charge"


def test_label_keys_are_data(model: SemanticModel, con: Any) -> None:
    """The question a flattened model cannot ask: which keys does spend carry?

    Answered for keys nobody declared, which is the whole argument for modelling
    a repeated column as an object rather than one column per known key.
    """
    by_key = _run(con, model, ["Label Key"], ["Billed Cost"])
    assert {"team", "env", "cost_center", "owner"} <= set(by_key)
    assert by_key["team"][0] > by_key["cost_center"][0] > 0


def test_a_parent_measure_is_not_double_counted(model: SemanticModel, con: Any) -> None:
    """``component`` and ``app`` often agree, so a value repeats on one charge.

    The naive unnest overstates ``worker`` by half. This is the number the whole
    showcase turns on, so it is checked against ground truth computed in SQL
    rather than against a recorded figure.
    """
    truth = float(
        con.execute(
            "SELECT SUM(BilledCost) FROM focus.charges "
            "WHERE list_contains(list_transform(Labels, x -> x.Value), 'worker')"
        ).fetchone()[0]
    )
    naive = float(
        con.execute(
            "SELECT SUM(BilledCost) FROM focus.charges, unnest(Labels) t(l) "
            "WHERE l.Value = 'worker'"
        ).fetchone()[0]
    )
    assert naive > truth * 1.4, "the fixture must actually exercise the double count"

    by_value = _run(con, model, ["Label Value"], ["Billed Cost"])
    assert by_value["worker"][0] == pytest.approx(truth, abs=0.01)


def test_a_nested_measure_keeps_its_duplicates(model: SemanticModel, con: Any) -> None:
    """The other direction: two identical credit lines are two credits.

    Deduplicating a nested-side measure the way a parent-side one is
    deduplicated would silently lose one of them.
    """
    truth = con.execute(
        "SELECT COUNT(*), SUM(c.Amount) FROM focus.charges, unnest(Credits) t(c) "
        "WHERE c.Type = 'COMMITTED_USAGE_DISCOUNT'"
    ).fetchone()
    by_type = _run(con, model, ["Credit Type"], ["Credit Amount", "Credit Line Count"])
    amount, lines = by_type["COMMITTED_USAGE_DISCOUNT"]
    assert lines == truth[0]
    assert amount == pytest.approx(float(truth[1]), abs=0.01)


def test_two_grains_in_one_result(model: SemanticModel, con: Any) -> None:
    """A credit-grain measure beside a charge-grain one, grouped by credit type.

    No single flat query produces both: the one that gets the credits right
    inflates the gross by counting a charge once per credit it carries.
    """
    by_type = _run(con, model, ["Credit Type"], ["Credit Amount", "Billed Cost"])
    credit, gross = by_type["COMMITTED_USAGE_DISCOUNT"]
    inflated = float(
        con.execute(
            "SELECT SUM(BilledCost) FROM focus.charges, unnest(Credits) t(c) "
            "WHERE c.Type = 'COMMITTED_USAGE_DISCOUNT'"
        ).fetchone()[0]
    )
    truth = float(
        con.execute(
            "SELECT SUM(BilledCost) FROM focus.charges "
            "WHERE list_contains(list_transform(Credits, x -> x.Type), "
            "'COMMITTED_USAGE_DISCOUNT')"
        ).fetchone()[0]
    )
    assert credit < 0
    assert gross == pytest.approx(truth, abs=0.01)
    assert inflated > truth, "the fixture must exercise the multi-credit charge"


def test_an_array_inside_a_struct(model: SemanticModel, con: Any) -> None:
    """``Project.Ancestors`` is reached by the dotted path, with no other help."""
    by_folder = _run(con, model, ["Org Folder"], ["Billed Cost"])
    assert {"Engineering", "Platform", "Data"} <= set(by_folder)
    # The root folder carries every charge that has a project at all.
    assert by_folder["Contoso"][0] > by_folder["Engineering"][0]


def test_the_untagged_share_is_visible(model: SemanticModel, con: Any) -> None:
    """A charge with no labels keeps its row, under a NULL label.

    The inner form of the unnest would drop it, and with it the answer to the
    question a FinOps model is bought for.
    """
    by_value = _run(con, model, ["Label Value"], ["Billed Cost"])
    assert None in by_value, "an untagged charge must survive the outer unnest"
    total = float(con.execute("SELECT SUM(BilledCost) FROM focus.charges").fetchone()[0])
    untagged = float(
        con.execute("SELECT SUM(BilledCost) FROM focus.charges WHERE len(Labels) = 0").fetchone()[0]
    )
    assert by_value[None][0] == pytest.approx(untagged, abs=0.01)
    assert 0.05 < untagged / total < 0.30
