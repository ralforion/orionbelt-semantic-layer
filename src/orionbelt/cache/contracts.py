"""Bridge between OBML ``RefreshPolicy`` (per dataObject) and TTL composition
(per physical table). See ``design/PLAN_freshness_driven_cache.md`` §5.5.
"""

from __future__ import annotations

from orionbelt.cache.ttl import RefreshContract, RefreshMode, compose_contracts, parse_duration
from orionbelt.models.errors import SemanticError
from orionbelt.models.semantic import RefreshPolicy, SemanticModel
from orionbelt.models.warnings import WarningCode, warning


def policy_to_contract(policy: RefreshPolicy | None) -> RefreshContract:
    """Convert a model-level :class:`RefreshPolicy` into the runtime contract.

    Invalid durations degrade to ``UNKNOWN`` rather than raising — the
    parser already records structured errors at load time.
    """
    if policy is None:
        return RefreshContract(mode=RefreshMode.UNKNOWN)
    mode = policy.mode.lower()
    try:
        if mode == "static":
            return RefreshContract(mode=RefreshMode.STATIC)
        if mode == "interval":
            seconds = parse_duration(policy.interval) if policy.interval else 0
            return RefreshContract(
                mode=RefreshMode.INTERVAL,
                interval_seconds=seconds,
                anchor=policy.anchor,
                timezone=policy.timezone or "UTC",
            )
        if mode == "heartbeat":
            seconds = parse_duration(policy.max_staleness) if policy.max_staleness else 0
            return RefreshContract(
                mode=RefreshMode.HEARTBEAT,
                max_staleness_seconds=seconds,
            )
    except ValueError:
        return RefreshContract(mode=RefreshMode.UNKNOWN)
    return RefreshContract(mode=RefreshMode.UNKNOWN)


def collect_table_contracts(
    model: SemanticModel,
) -> tuple[dict[str, RefreshContract], list[SemanticError]]:
    """Group dataObjects by physical table; compose per-table contracts.

    Returns ``(table_ref → composed contract, warnings)``. When two
    dataObjects sharing a physical table declare different refresh
    contracts, the strictest wins and a structured warning is emitted
    (``SHARED_TABLE_CONTRACT_DISAGREEMENT``).
    """
    grouped: dict[str, list[tuple[str, RefreshContract]]] = {}
    for name, obj in model.data_objects.items():
        parts = [str(p) for p in (obj.database, obj.schema_name, obj.code) if p]
        if not parts:
            continue
        table_ref = ".".join(parts)
        contract = policy_to_contract(obj.refresh)
        grouped.setdefault(table_ref, []).append((name, contract))

    contracts: dict[str, RefreshContract] = {}
    warnings: list[SemanticError] = []
    for table_ref, entries in grouped.items():
        labelled = [c for _, c in entries]
        first = labelled[0]
        disagreement = any(c != first for c in labelled[1:])
        composed = compose_contracts(labelled)
        contracts[table_ref] = composed
        if disagreement:
            warnings.append(
                warning(
                    code=WarningCode.SHARED_TABLE_CONTRACT_DISAGREEMENT,
                    message=(
                        f"DataObjects {[n for n, _ in entries]} map to physical table "
                        f"'{table_ref}' but declare disagreeing refresh contracts; "
                        "OBSL applied the strictest."
                    ),
                    path=f"dataObjects[{','.join(n for n, _ in entries)}].refresh",
                    hint=(
                        "Make refresh blocks identical across dataObjects on the same table, "
                        "or move the contract onto a single canonical dataObject."
                    ),
                    context={
                        "table_ref": table_ref,
                        "data_objects": [n for n, _ in entries],
                    },
                )
            )
    return contracts, warnings
