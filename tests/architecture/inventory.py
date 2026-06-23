"""Architecture inventory for ``src/orionbelt`` (Phase 0 guardrail).

Plan §"Phase 0 - Baseline and guardrails" asks for an inventory that
records the architectural facts most likely to regress as the codebase
evolves:

  * largest modules (the "gravity wells" the plan calls out),
  * import cycles inside the package,
  * ``RawSQL`` construction sites (the dialect escape hatch),
  * broad ``except`` sites outside the approved boundary modules.

This module is intentionally **measurement only** — it computes the
inventory and renders a stable, sorted report. It makes no policy
decisions and raises nothing on its own. The accompanying test keeps it
informational (Phase 0 must not fail CI); later phases (§5 quality gates,
§6 RawSQL containment) can turn individual measurements into hard gates
by reading the same dataclasses.

Everything here is computed from the source tree with ``ast`` so the
output does not depend on an importable/installed package, and is sorted
deterministically so CI logs diff cleanly between runs.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

# Repo root: tests/architecture/inventory.py -> repo root is two parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "orionbelt"
PACKAGE = "orionbelt"

# Boundary modules legitimately translate the outside world (HTTP, wire
# protocols, filesystem caches, external DB drivers, user-supplied YAML)
# and so are *expected* to use broad ``except`` clauses defensively. Broad
# excepts in the core compiler/dialect/model layers are the interesting
# ones, so the inventory reports those separately.
BOUNDARY_PREFIXES: tuple[str, ...] = (
    "orionbelt.api",
    "orionbelt.auth",
    "orionbelt.cache",
    "orionbelt.pgwire",
    "orionbelt.parser",
    "orionbelt.service",
    "orionbelt.ui",
)


@dataclass(frozen=True)
class ModuleSize:
    """Line count for a single source module."""

    module: str
    path: str
    lines: int


@dataclass(frozen=True)
class CallSite:
    """A single source location (module + line) of interest."""

    module: str
    path: str
    line: int
    detail: str = ""


@dataclass(frozen=True)
class Inventory:
    """The full architectural snapshot."""

    module_sizes: list[ModuleSize] = field(default_factory=list)
    import_cycles: list[tuple[str, ...]] = field(default_factory=list)
    raw_sql_sites: list[CallSite] = field(default_factory=list)
    broad_except_sites: list[CallSite] = field(default_factory=list)

    @property
    def core_broad_except_sites(self) -> list[CallSite]:
        """Broad ``except`` sites outside the approved boundary modules."""
        return [s for s in self.broad_except_sites if not _is_boundary(s.module)]


def _is_boundary(module: str) -> bool:
    return any(module == p or module.startswith(p + ".") for p in BOUNDARY_PREFIXES)


def _iter_source_files() -> list[Path]:
    return sorted(p for p in SRC_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _module_name(path: Path) -> str:
    """Dotted module name for a file under ``src/orionbelt``."""
    rel = path.relative_to(SRC_ROOT.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _safe_parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
        return None


def _resolve_import(target: str, known: set[str]) -> str | None:
    """Map an imported dotted name to the longest known internal module.

    A submodule import (``orionbelt.compiler.star``) resolves to itself if
    it is a tracked module, otherwise to the nearest enclosing package
    (``orionbelt.compiler``). Returns ``None`` if nothing internal matches.
    """
    parts = target.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in known:
            return candidate
        parts.pop()
    return None


def _internal(name: str) -> bool:
    return name == PACKAGE or name.startswith(PACKAGE + ".")


def _import_targets(node: ast.Import | ast.ImportFrom, current: str, known: set[str]) -> list[str]:
    """Internal modules a single import statement depends on, resolved.

    A ``from pkg import sub`` where ``pkg.sub`` is a known module is treated
    as a dependency on the **submodule** (``pkg.sub``), not on ``pkg``'s
    ``__init__``. Resolving it to the package would manufacture spurious
    cycles for the standard "package re-exports its submodules" pattern.
    """
    resolved: list[str] = []

    def add(target: str) -> None:
        if not _internal(target):
            return
        hit = _resolve_import(target, known)
        if hit:
            resolved.append(hit)

    if isinstance(node, ast.Import):
        for alias in node.names:
            add(alias.name)
        return resolved

    # ast.ImportFrom
    if node.level:
        # Relative import: resolve against the current module's package.
        base_parts = current.split(".")
        # Drop ``level`` trailing components: ``from . import x`` (level 1)
        # targets a sibling of the current module.
        base_parts = base_parts[: len(base_parts) - node.level]
        prefix = ".".join(base_parts)
        module = f"{prefix}.{node.module}" if node.module else prefix
    else:
        module = node.module or ""
    if not module:
        return resolved
    for alias in node.names:
        submodule = f"{module}.{alias.name}"
        if submodule in known:
            add(submodule)  # importing a submodule
        else:
            add(module)  # importing a symbol defined in ``module``
    return resolved


def _is_type_checking_guard(node: ast.If) -> bool:
    """True for an ``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:`` block."""
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _iter_import_time_imports(body: list[ast.stmt]) -> list[ast.Import | ast.ImportFrom]:
    """Imports that actually execute at module import time.

    Recurses through statements evaluated when the module is imported
    (module scope, class bodies, top-level ``if``/``try``/``with``/loop
    blocks) but deliberately skips:

      * function / async-function bodies — those imports are *deferred*
        and are the project's standard tool for breaking cycles, so they
        must not register as import-time edges;
      * ``if TYPE_CHECKING:`` blocks — type-only imports never run.
    """
    found: list[ast.Import | ast.ImportFrom] = []
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue  # deferred — not an import-time edge
        elif isinstance(node, ast.If):
            # ``if TYPE_CHECKING:`` body is type-only and never runs, but its
            # ``else:`` (the runtime branch) still executes at import time.
            if not _is_type_checking_guard(node):
                found.extend(_iter_import_time_imports(node.body))
            found.extend(_iter_import_time_imports(node.orelse))
        elif isinstance(node, ast.ClassDef):
            found.extend(_iter_import_time_imports(node.body))
        elif isinstance(node, ast.Try):
            found.extend(_iter_import_time_imports(node.body))
            for handler in node.handlers:
                found.extend(_iter_import_time_imports(handler.body))
            found.extend(_iter_import_time_imports(node.orelse))
            found.extend(_iter_import_time_imports(node.finalbody))
        elif isinstance(node, (ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)):
            found.extend(_iter_import_time_imports(node.body))
    return found


def _collect_import_edges(tree: ast.Module, current: str, known: set[str]) -> set[str]:
    edges: set[str] = set()
    for node in _iter_import_time_imports(tree.body):
        for resolved in _import_targets(node, current, known):
            if resolved != current:
                edges.add(resolved)
    return edges


_BROAD_NAMES = frozenset({"Exception", "BaseException"})


def _names_exception(node: ast.expr) -> bool:
    """True if a caught-type expression refers to ``Exception``/``BaseException``."""
    if isinstance(node, ast.Name):
        return node.id in _BROAD_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in _BROAD_NAMES
    if isinstance(node, ast.Tuple):
        # ``except (ValueError, Exception):`` is still broad.
        return any(_names_exception(elt) for elt in node.elts)
    return False


def _is_broad_except(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True  # bare ``except:``
    return _names_exception(handler.type)


def _is_raw_sql_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "RawSQL"
    if isinstance(func, ast.Attribute):
        return func.attr == "RawSQL"
    return False


def import_edges() -> set[tuple[str, str]]:
    """Internal import-time module dependency edges, ``(source, target)``.

    Same edge model the cycle detector uses: only imports that execute at
    import time (no deferred function-local or ``TYPE_CHECKING`` imports),
    resolved to known internal modules. Used by the dependency-direction
    architecture test.
    """
    files = _iter_source_files()
    known = {_module_name(p) for p in files}
    edges: set[tuple[str, str]] = set()
    for path in files:
        module = _module_name(path)
        tree = _safe_parse(path)
        if tree is None:
            continue
        for target in _collect_import_edges(tree, module, known):
            edges.add((module, target))
    return edges


def build_inventory(*, top_n: int = 15) -> Inventory:
    """Compute the architecture inventory from the source tree."""
    files = _iter_source_files()
    known = {_module_name(p) for p in files}

    module_sizes: list[ModuleSize] = []
    raw_sql_sites: list[CallSite] = []
    broad_except_sites: list[CallSite] = []
    graph: nx.DiGraph[str] = nx.DiGraph()
    graph.add_nodes_from(known)

    for path in files:
        module = _module_name(path)
        rel = str(path.relative_to(REPO_ROOT))
        text = path.read_text(encoding="utf-8")
        # ``splitlines()`` matches ``wc -l`` for the usual newline-terminated
        # file (no phantom trailing line from a final ``\n``).
        module_sizes.append(ModuleSize(module=module, path=rel, lines=len(text.splitlines())))

        tree = _safe_parse(path)
        if tree is None:
            continue

        for target in _collect_import_edges(tree, module, known):
            graph.add_edge(module, target)

        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and _is_broad_except(node):
                broad_except_sites.append(CallSite(module=module, path=rel, line=node.lineno))
            elif isinstance(node, ast.Call) and _is_raw_sql_call(node):
                raw_sql_sites.append(CallSite(module=module, path=rel, line=node.lineno))

    # Normalise cycles so the report is order-stable: rotate each cycle to
    # start at its lexicographically smallest node, then sort the list.
    cycles: list[tuple[str, ...]] = []
    for cycle in nx.simple_cycles(graph):
        if len(cycle) < 2:
            continue  # self-loops are not cross-module cycles
        start = cycle.index(min(cycle))
        cycles.append(tuple(cycle[start:] + cycle[:start]))
    cycles.sort()

    module_sizes.sort(key=lambda m: (-m.lines, m.module))
    raw_sql_sites.sort(key=lambda s: (s.path, s.line))
    broad_except_sites.sort(key=lambda s: (s.path, s.line))

    return Inventory(
        module_sizes=module_sizes[:top_n],
        import_cycles=cycles,
        raw_sql_sites=raw_sql_sites,
        broad_except_sites=broad_except_sites,
    )


def format_report(inv: Inventory) -> str:
    """Render a stable, human-readable report for CI logs."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("ORIONBELT ARCHITECTURE INVENTORY (informational, Phase 0)")
    lines.append("=" * 72)

    lines.append("")
    lines.append(f"Largest modules (top {len(inv.module_sizes)}):")
    for m in inv.module_sizes:
        lines.append(f"  {m.lines:>5}  {m.path}")

    lines.append("")
    lines.append(f"Import cycles in {PACKAGE}: {len(inv.import_cycles)}")
    for cycle in inv.import_cycles:
        lines.append("  " + " -> ".join(cycle) + f" -> {cycle[0]}")

    lines.append("")
    lines.append(f"RawSQL construction sites: {len(inv.raw_sql_sites)}")
    for s in inv.raw_sql_sites:
        lines.append(f"  {s.path}:{s.line}")

    core = inv.core_broad_except_sites
    lines.append("")
    lines.append(
        f"Broad except sites: {len(inv.broad_except_sites)} total, "
        f"{len(core)} outside boundary modules"
    )
    for s in core:
        lines.append(f"  {s.path}:{s.line}")

    lines.append("=" * 72)
    return "\n".join(lines)
