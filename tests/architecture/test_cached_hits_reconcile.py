"""Every rebuild of a cached result must reconcile it.

This rule was rediscovered four times, once per read path, because each site
looks correct on its own: it calls ``execution_result_from_data`` and then a
response builder that *does* reconcile. The trap was that the rebuilt result
used to be row-backed while ``reconcile_to_declared`` needs an Arrow table, so
reconciling was a silent no-op and a hit returned the engine's types while the
miss that filled the entry returned the model's.

A rebuilt hit holds its table now, so the call works wherever it is made - but
it still has to be *made*, and a reader who does not know the history has no
reason to think of it. The response builder cannot be relied on either: the
read path hands it the skips, so it takes them rather than re-deriving them.

So the invariant stays checked structurally rather than left to be remembered:
a function that rebuilds a cached result must also name
``reconcile_to_declared``.
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


#: The response builder that turns skips into DECLARED_TYPE_NOT_APPLIED warnings.
_BUILDER = "_build_execute_response"


def test_a_rebuild_site_hands_its_skips_to_the_builder() -> None:
    """Reconciling is half of it; the warnings have to reach the response.

    The first version of this rule only checked that a rebuild site *named*
    ``reconcile_to_declared``. Oneshot did - it computed ``hit_skips`` and then
    never passed them - and the guard stayed green while the response reported
    ``type=boolean`` over rows of 0/1/7 with no warning at all. The builder
    cannot rederive them: the rebuilt result is row-backed, so its own
    reconciliation returns nothing.

    Scoped to the builder call that *consumes the rebuilt result*. A miss in
    the same function passes no skips on purpose - there the builder holds an
    Arrow table and derives them itself - so demanding the argument everywhere
    would force a wrong one.
    """
    offenders = []
    for path, fn in _rebuild_sites():
        rebuilt = {
            node.targets[0].id
            for node in ast.walk(fn)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == _REBUILD
        }
        for call in ast.walk(fn):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == _BUILDER
            ):
                continue
            consumes = any(
                kw.arg == "exec_result"
                and isinstance(kw.value, ast.Name)
                and kw.value.id in rebuilt
                for kw in call.keywords
            )
            if consumes and not any(kw.arg == "declared_skips" for kw in call.keywords):
                offenders.append(f"{path.relative_to(SRC)}::{fn.name}")
    assert not offenders, (
        "these hand a rebuilt cached result to the response builder without "
        "declared_skips, so the hit reports no DECLARED_TYPE_NOT_APPLIED warning "
        "even though it computed one: " + ", ".join(offenders)
    )
