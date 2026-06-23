"""Architecture dependency-direction gate (Phase 7.3).

Enforces the layering of ``src/orionbelt``: a lower layer must not import a
higher one. The rules below encode the *forbidden* directions and are
derived from the current (clean) import graph — every rule holds today, so
a new edge that inverts a layer (e.g. ``compiler`` importing ``api``, or
``models`` importing ``compiler``) fails this test.

Edges are import-time only (deferred function-local / ``TYPE_CHECKING``
imports are excluded), matching the cycle detector. Cheap enough for every
PR.
"""

from __future__ import annotations

import ast

from tests.architecture.inventory import _collect_import_edges, import_edges

# For each subpackage, the set of subpackages it must NOT import. The layering
# (low -> high): ast/models -> dialect -> compiler -> {cache,obsl,parser} ->
# service -> api -> pgwire; ui is a thin client over service. ``auth`` and
# ``settings`` are leaf config consumed widely and are not constrained here.
# Everything above the data layer; the two leaf layers (ast, models) may not
# import any of these (nor each other).
_ABOVE_DATA = {"dialect", "compiler", "parser", "cache", "obsl", "service", "api", "pgwire", "ui"}

FORBIDDEN: dict[str, set[str]] = {
    "ast": _ABOVE_DATA | {"models"},
    "models": _ABOVE_DATA | {"ast"},
    "dialect": {"compiler", "parser", "cache", "obsl", "service", "api", "pgwire", "ui"},
    "compiler": {"parser", "cache", "obsl", "service", "api", "pgwire", "ui"},
    "parser": {"dialect", "compiler", "cache", "obsl", "service", "api", "pgwire", "ui"},
    "cache": {"dialect", "compiler", "parser", "obsl", "service", "api", "pgwire", "ui"},
    "obsl": {"dialect", "compiler", "parser", "cache", "service", "api", "pgwire", "ui"},
    "service": {"api", "pgwire", "ui"},
    "api": {"pgwire", "ui"},
    "ui": {"pgwire"},
}


def _subpackage(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else module


def test_no_forbidden_layer_imports() -> None:
    violations: list[str] = []
    for source, target in import_edges():
        src, dst = _subpackage(source), _subpackage(target)
        if src == dst:
            continue
        if dst in FORBIDDEN.get(src, set()):
            violations.append(f"{source} -> {target}  ({src} must not import {dst})")
    assert not violations, "Forbidden import directions found:\n" + "\n".join(sorted(violations))


def test_relative_import_from_package_init_is_resolved() -> None:
    """Regression: a relative import in a package ``__init__`` must produce an edge.

    ``from .. import compiler`` inside ``orionbelt/models/__init__.py`` targets
    ``orionbelt.compiler`` — it must not be silently dropped (which would let a
    forbidden models -> compiler edge bypass the layering gate).
    """
    known = {"orionbelt.models", "orionbelt.compiler"}
    tree = ast.parse("from .. import compiler\n")
    edges = _collect_import_edges(tree, "orionbelt.models", known, is_package=True)
    assert "orionbelt.compiler" in edges

    # The same statement in a regular module ``orionbelt.models.semantic``
    # anchors at the parent, so ``..`` reaches ``orionbelt`` -> still compiler.
    edges_mod = _collect_import_edges(tree, "orionbelt.models.semantic", known, is_package=False)
    assert "orionbelt.compiler" in edges_mod

    # ``from . import x`` in the package targets the package itself, not a sibling.
    known2 = {"orionbelt.models", "orionbelt.models.helpers"}
    tree2 = ast.parse("from . import helpers\n")
    edges2 = _collect_import_edges(tree2, "orionbelt.models", known2, is_package=True)
    assert "orionbelt.models.helpers" in edges2
