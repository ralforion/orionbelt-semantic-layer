"""Broad-except growth guard (Phase 7.4).

Boundary modules (HTTP, wire protocols, caches, DB drivers, the YAML
parser) legitimately catch broadly; the core compiler/dialect/model layers
should not. This gate fails if broad ``except Exception`` / bare ``except``
sites *outside* the approved boundary modules grow beyond the current
baseline, forcing a deliberate choice: narrow the exception, move the code
to a boundary module, or raise the baseline with a justification.

Counting (not line numbers) keeps the guard robust to unrelated edits that
shift line positions. ``inventory.BOUNDARY_PREFIXES`` defines which modules
are exempt.
"""

from __future__ import annotations

from tests.architecture.inventory import build_inventory

# Current core (non-boundary) broad-except sites. As of this baseline they
# live in the compiler resolution layer:
#   compiler/resolution.py:88            (noqa: BLE001 — preserve prior behaviour)
#   compiler/metric_resolution.py x2     (expression-resolution fallbacks)
# Lower this number as the exceptions are narrowed; raise it only with a
# documented reason in the PR.
BASELINE_CORE_BROAD_EXCEPT = 3


def test_core_broad_except_does_not_grow() -> None:
    inv = build_inventory()
    sites = inv.core_broad_except_sites
    listing = "\n".join(f"  {s.path}:{s.line}" for s in sites)
    assert len(sites) <= BASELINE_CORE_BROAD_EXCEPT, (
        f"Broad `except` outside boundary modules grew to {len(sites)} "
        f"(baseline {BASELINE_CORE_BROAD_EXCEPT}). Narrow the exception, move it to a "
        f"boundary module, or raise BASELINE_CORE_BROAD_EXCEPT with a reason.\n"
        f"Current core sites:\n{listing}"
    )
