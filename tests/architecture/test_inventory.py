"""Phase 0 architecture inventory — informational, never fails CI.

This test computes the architecture inventory (largest modules, import
cycles, ``RawSQL`` sites, broad ``except`` sites) and stashes a rendered
report so the session terminal summary can print it (see ``conftest.py``).

Per the architecture improvement plan, Phase 0 is a *baseline*: the
inventory is recorded and made easy to inspect, but it does **not** assert
thresholds. The only assertions here guard the measurement itself (the
inventory is computable and self-consistent), not the codebase shape.
Turning specific measurements into hard gates is deferred to later phases
(quality gates and RawSQL containment).
"""

from __future__ import annotations

import ast

import pytest

from tests.architecture.conftest import INVENTORY_REPORT_KEY
from tests.architecture.inventory import (
    SRC_ROOT,
    Inventory,
    _collect_import_edges,
    _is_broad_except,
    build_inventory,
    format_report,
)


def _only_handler(src: str) -> ast.ExceptHandler:
    tree = ast.parse(src)
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert len(handlers) == 1
    return handlers[0]


@pytest.mark.parametrize(
    "src",
    [
        "try:\n    x()\nexcept Exception:\n    pass",
        "try:\n    x()\nexcept BaseException:\n    pass",
        "try:\n    x()\nexcept:\n    pass",
        "try:\n    x()\nexcept (ValueError, Exception):\n    pass",  # tuple form
        "try:\n    x()\nexcept (KeyError, BaseException) as e:\n    pass",
    ],
)
def test_broad_except_detected(src: str) -> None:
    assert _is_broad_except(_only_handler(src)) is True


@pytest.mark.parametrize(
    "src",
    [
        "try:\n    x()\nexcept ValueError:\n    pass",
        "try:\n    x()\nexcept (KeyError, ValueError):\n    pass",
    ],
)
def test_narrow_except_not_flagged(src: str) -> None:
    assert _is_broad_except(_only_handler(src)) is False


def test_type_checking_else_branch_is_import_time() -> None:
    """The runtime ``else:`` of an ``if TYPE_CHECKING`` block must be walked."""
    src = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from orionbelt.models.semantic import SemanticModel\n"
        "else:\n"
        "    from orionbelt.compiler import resolution\n"
    )
    tree = ast.parse(src)
    known = {"orionbelt.models.semantic", "orionbelt.compiler.resolution"}
    edges = _collect_import_edges(tree, "orionbelt.example", known)
    # The TYPE_CHECKING-only import is excluded; the else-branch import is kept.
    assert "orionbelt.compiler.resolution" in edges
    assert "orionbelt.models.semantic" not in edges


@pytest.fixture(scope="session")
def inventory(request: pytest.FixtureRequest) -> Inventory:
    inv = build_inventory()
    # Stash the rendered report for the terminal-summary hook in conftest.
    request.config.stash[INVENTORY_REPORT_KEY] = format_report(inv)
    return inv


def test_source_tree_present() -> None:
    """Sanity check: the package we are measuring exists."""
    assert SRC_ROOT.is_dir(), f"expected source tree at {SRC_ROOT}"


def test_inventory_is_computable(inventory: Inventory) -> None:
    """The inventory builds and reports a non-empty set of modules."""
    assert inventory.module_sizes, "no source modules were measured"
    # Modules are reported largest-first.
    sizes = [m.lines for m in inventory.module_sizes]
    assert sizes == sorted(sizes, reverse=True)


def test_line_counts_match_wc(inventory: Inventory) -> None:
    """Reported line counts match ``wc -l`` (newline count) for each module."""
    repo_root = SRC_ROOT.parents[1]
    for m in inventory.module_sizes:
        text = (repo_root / m.path).read_text(encoding="utf-8")
        assert m.lines == text.count("\n"), m.path


def test_report_renders(inventory: Inventory) -> None:
    """The report renders deterministically and mentions each section."""
    report = format_report(inventory)
    assert "ARCHITECTURE INVENTORY" in report
    assert "Largest modules" in report
    assert "Import cycles" in report
    assert "RawSQL construction sites" in report
    assert "Broad except sites" in report
    # Rendering is pure: same inventory -> identical report.
    assert format_report(inventory) == report


def test_inventory_is_informational(
    inventory: Inventory, capsys: pytest.CaptureFixture[str]
) -> None:
    """Phase 0 records the inventory without enforcing thresholds.

    This test never fails on the *shape* of the codebase. It always prints
    the report (so a developer running ``pytest -s`` sees it inline) and
    documents the current baseline counts in its captured output.
    """
    print(format_report(inventory))
    # Informational assertions only: the collections are well-formed lists.
    assert isinstance(inventory.import_cycles, list)
    assert isinstance(inventory.raw_sql_sites, list)
    assert isinstance(inventory.core_broad_except_sites, list)
    # Ensure the print produced output (keeps the report path exercised).
    assert capsys.readouterr().out
