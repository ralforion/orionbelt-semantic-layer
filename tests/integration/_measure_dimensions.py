"""Which dimensions a measure cannot be queried without.

Both measure sweeps ask every measure of the commerce model to execute, one
against the local vendor containers and one against live Dremio through pgwire,
and both hit the same wall: a measure whose grain is fixed to a dimension list
cannot answer a grand total. Resolution refuses it, because aggregating at a
grain the query does not group by is what multiplies rows.

That is a bad question rather than a vendor bug, so the sweeps ask the right one
instead of skipping the measure - the grain path is exactly the kind of SQL they
exist to execute against a real engine. The rule lives here so the two cannot
drift, which they already did once: the fix landed in one sweep while the other
went on failing.
"""

from __future__ import annotations

from typing import Any

from orionbelt.models.semantic import GrainMode


def required_dimensions(measure: Any) -> list[str]:
    """The dimensions a query must group by for *measure* to be answerable."""
    grain = getattr(measure, "grain", None)
    if grain is None or grain.mode != GrainMode.FIXED:
        return []
    return [*grain.include, *grain.keep_only]
