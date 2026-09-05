"""Every rebuild of a cached result must reconcile the table first.

This rule was rediscovered four times, once per read path, because each site
looks correct on its own: it calls ``execution_result_from_data`` and then a
response builder that *does* reconcile. The trap is that the rebuilt result is
row-backed - ``table_to_rows`` keeps native dates where the Arrow row builder
serialises them, so a hit cannot simply hold the table - and
``ExecutionResult.reconcile_to_declared`` needs an Arrow table. Reconciling the
result is therefore a silent no-op, and the hit returns the engine's types
while the miss that filled the entry returned the model's.

So the invariant is checked structurally rather than left to be remembered: a
function that rebuilds a cached result must also name ``reconcile_to_declared``.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "orionbelt"

#: The call that rebuilds an ``ExecutionResult`` from a cached data table.
_REBUILD = "execution_result_from_data"
_RECONCILE = "reconcile_to_declared"


def _functions_calling(tree: ast.AST, name: str) -> list[ast.AST]:
    """Every function definition whose body calls *name*."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == name
            ):
                found.append(node)
                break
    return found


def _rebuild_sites() -> list[tuple[pathlib.Path, ast.AST]]:
    sites = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for fn in _functions_calling(tree, _REBUILD):
            sites.append((path, fn))
    return sites


def test_there_are_rebuild_sites_to_check() -> None:
    """Guards the guard: a renamed helper would silently pass everything."""
    assert _rebuild_sites(), f"no call sites of {_REBUILD} found - has it been renamed?"


def test_every_cached_rebuild_reconciles_the_table() -> None:
    offenders = []
    for path, fn in _rebuild_sites():
        names = {
            inner.func.id
            for inner in ast.walk(fn)
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
        }
        attrs = {
            inner.func.attr
            for inner in ast.walk(fn)
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
        }
        if _RECONCILE not in names | attrs:
            offenders.append(f"{path.relative_to(SRC)}::{fn.name}")
    assert not offenders, (
        "these rebuild a cached result without reconciling the table first, so the "
        "hit returns the engine's types while its miss returned the model's: "
        + ", ".join(offenders)
    )
