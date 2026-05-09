"""Tier 1 — HAVING auto-include.

Regression check for Finding 2 in
``design/PLAN_compiler_semantic_findings.md``: a HAVING filter that
references a measure not in ``select.measures`` must compile to valid
SQL whose row set matches the equivalent query that explicitly
projects the same measure.

The auto-include lives in ``compiler/resolution.py`` — the resolver
pre-scans HAVING for measure/metric references and adds any unseen
ones to ``resolved.measures`` (also tracking them in
``resolved.having_only_measures``) before base-object selection so
the multi-fact CFL trigger sees them.

Cosmetic note: today's implementation projects the auto-included
measure in the final SELECT (an extra column the user didn't ask
for). This test treats that as an accepted behaviour and asserts the
*core* property — that the user-requested measures and the row keys
agree between the two query forms.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from orionbelt.models.query import (
    FilterOperator,
    QueryFilter,
    QueryObject,
    QuerySelect,
)


def _to_decimal(v: Any) -> Decimal:
    if v is None:
        return Decimal(0)
    return v if isinstance(v, Decimal) else Decimal(str(v))


def test_having_on_non_selected_measure_matches_explicit_form(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
) -> None:
    """``Total Sales by Client Name HAVING Complaint Count > 0`` is well-formed.

    Compares two forms that should produce the same per-client Total
    Sales values:

    * **Implicit** — ``select.measures = ["Total Sales"]``,
      ``HAVING Complaint Count > 0``. Pre-fix this emitted invalid SQL
      (binder error). Post-fix the resolver auto-includes
      ``Complaint Count`` in the projection.
    * **Explicit** — ``select.measures = ["Total Sales", "Complaint Count"]``,
      same HAVING. Always compiled.

    Both forms should yield the same ``(Client Name, Total Sales)``
    pairs across the same set of clients.
    """
    having = [QueryFilter(field="Complaint Count", op=FilterOperator.GT, value=0)]

    implicit = run_query(
        QueryObject(
            select=QuerySelect(
                dimensions=["Client Name"],
                measures=["Total Sales"],
            ),
            having=having,
        )
    )
    explicit = run_query(
        QueryObject(
            select=QuerySelect(
                dimensions=["Client Name"],
                measures=["Total Sales", "Complaint Count"],
            ),
            having=having,
        )
    )

    impl_by_client = {r["Client Name"]: _to_decimal(r["Total Sales"]) for r in implicit}
    expl_by_client = {r["Client Name"]: _to_decimal(r["Total Sales"]) for r in explicit}

    assert set(impl_by_client) == set(expl_by_client), (
        f"Client sets differ between HAVING forms: "
        f"only-in-implicit={set(impl_by_client) - set(expl_by_client)}, "
        f"only-in-explicit={set(expl_by_client) - set(impl_by_client)}. "
        f"Suggests the auto-include changed the planner's CFL or "
        f"join-graph reasoning vs the explicit form."
    )

    for client, impl_total in impl_by_client.items():
        assert impl_total == expl_by_client[client], (
            f"Client {client!r}: Total Sales differs — implicit "
            f"HAVING form gave {impl_total}, explicit form gave "
            f"{expl_by_client[client]}. The auto-include should not "
            f"change the per-client aggregate."
        )

    # Sanity: at least one row is left after HAVING — otherwise the
    # invariant is trivially satisfied and would mask a bug.
    assert impl_by_client, (
        "HAVING Complaint Count > 0 should match at least one client; "
        "got an empty result. Possibly the auto-included measure was "
        "filtered out by an earlier resolution pass."
    )
