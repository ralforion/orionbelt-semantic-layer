"""TTL composition over the physical tables a query touches.

See ``design/PLAN_freshness_driven_cache.md`` §8. The planner identifies the
deduplicated set of ``(database, schema, code)`` triples; this module turns
that set + per-table refresh contracts into a single effective TTL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class RefreshMode(StrEnum):
    """Refresh contract modes declared on a dataObject's source table."""

    INTERVAL = "interval"
    HEARTBEAT = "heartbeat"
    STATIC = "static"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RefreshContract:
    """A frozen view of a physical table's freshness contract.

    Built from one or more :class:`orionbelt.models.semantic.RefreshPolicy`
    entries on dataObjects that map to the same physical table. When two
    dataObjects on the same table disagree, the strictest contract wins
    (see ``compose_contracts``).
    """

    mode: RefreshMode
    interval_seconds: int | None = None
    anchor: str | None = None  # "HH:MM"
    timezone: str | None = None
    max_staleness_seconds: int | None = None


class NoCacheReason(StrEnum):
    """Why a query is not cacheable."""

    UNKNOWN_FRESHNESS = "unknown_freshness"
    BELOW_MIN_TTL = "below_min_ttl"
    ZERO_DERIVED_TTL = "zero_derived_ttl"
    NON_DETERMINISTIC_SQL = "non_deterministic_sql"


@dataclass(frozen=True)
class TtlComputation:
    """Successful TTL composition output."""

    seconds: int
    derived_seconds: int
    caller_seconds: int | None
    source: str  # "freshness_derived" | "caller_capped" | "default_unknown" | "all_static"
    limiting_table: str | None
    limiting_reason: str | None  # "interval" | "heartbeat" | "default_unknown"


@dataclass(frozen=True)
class TtlResult:
    """Outcome of TTL computation: either cacheable for ``ttl`` seconds or not.

    ``ttl`` is None when the query should not be cached. ``no_cache_reason``
    documents why so the response can surface a structured warning.
    """

    ttl: TtlComputation | None
    no_cache_reason: NoCacheReason | None = None
    no_cache_table: str | None = None


def parse_duration(text: str) -> int:
    """Parse a duration string into seconds.

    Accepts the OBML shorthand (``5s``, ``15m``, ``1h``, ``1d``) and ISO 8601
    durations (``PT5M``, ``P1D``, ``PT1H30M``). Sub-second values are
    rejected so refresh contracts never round to zero.
    """
    if not text:
        raise ValueError("empty duration")
    s = text.strip()
    if s.startswith(("P", "p")):
        return _parse_iso_duration(s)
    return _parse_shorthand_duration(s)


_SHORTHAND_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _parse_shorthand_duration(s: str) -> int:
    """Parse ``5m``, ``1h``, ``1d`` style durations."""
    if not s or not s[-1].isalpha():
        raise ValueError(f"invalid duration: {s!r}")
    unit = s[-1].lower()
    if unit not in _SHORTHAND_UNITS:
        raise ValueError(f"unsupported duration unit: {unit!r}")
    try:
        n = int(s[:-1])
    except ValueError as exc:
        raise ValueError(f"invalid duration number in {s!r}") from exc
    if n <= 0:
        raise ValueError(f"duration must be positive: {s!r}")
    seconds = n * _SHORTHAND_UNITS[unit]
    if seconds < 1:
        raise ValueError(f"sub-second durations not allowed: {s!r}")
    return seconds


def _parse_iso_duration(s: str) -> int:
    """Parse a subset of ISO 8601 durations (no months/years)."""
    s = s.upper()
    if not s.startswith("P"):
        raise ValueError(f"ISO duration must start with 'P': {s!r}")
    rest = s[1:]
    days = hours = minutes = seconds = 0
    in_time = False
    buf = ""
    for ch in rest:
        if ch == "T":
            in_time = True
            continue
        if ch.isdigit():
            buf += ch
            continue
        if not buf:
            raise ValueError(f"missing number before {ch!r} in {s!r}")
        n = int(buf)
        buf = ""
        if not in_time and ch == "D":
            days = n
        elif in_time and ch == "H":
            hours = n
        elif in_time and ch == "M":
            minutes = n
        elif in_time and ch == "S":
            seconds = n
        else:
            raise ValueError(f"unsupported ISO duration component {ch!r} in {s!r}")
    if buf:
        raise ValueError(f"trailing number without unit in {s!r}")
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    if total <= 0:
        raise ValueError(f"duration must be positive: {s!r}")
    return total


_STRICTNESS = {
    RefreshMode.UNKNOWN: 4,
    RefreshMode.HEARTBEAT: 3,
    RefreshMode.INTERVAL: 2,
    RefreshMode.STATIC: 1,
}


def is_stricter(a: RefreshContract, b: RefreshContract) -> bool:
    """Return True if ``a`` is strictly stricter than ``b``.

    Order: ``unknown`` > ``heartbeat`` > ``interval`` > ``static``. Within
    the same mode, the smaller window wins (shorter interval, smaller
    max_staleness).
    """
    if _STRICTNESS[a.mode] != _STRICTNESS[b.mode]:
        return _STRICTNESS[a.mode] > _STRICTNESS[b.mode]
    if a.mode == RefreshMode.INTERVAL:
        return (a.interval_seconds or 0) < (b.interval_seconds or 0)
    if a.mode == RefreshMode.HEARTBEAT:
        return (a.max_staleness_seconds or 0) < (b.max_staleness_seconds or 0)
    return False


def compose_contracts(contracts: list[RefreshContract]) -> RefreshContract:
    """Pick the strictest contract from a list (cross-dataObject merge)."""
    if not contracts:
        return RefreshContract(mode=RefreshMode.UNKNOWN)
    best = contracts[0]
    for c in contracts[1:]:
        if is_stricter(c, best):
            best = c
    return best


def _seconds_until_next_refresh(
    contract: RefreshContract,
    table_ref: str,
    now: datetime,
    heartbeats: dict[str, datetime],
) -> int:
    """Seconds until the next refresh tick for an interval-mode contract.

    With no anchor: ``last_observed + interval - now`` (defaulting to a
    fresh interval if the table was never heartbeated).
    With an anchor: align to the next ``HH:MM`` boundary in ``timezone``.
    """
    interval = contract.interval_seconds or 0
    if interval <= 0:
        return 0

    if contract.anchor and contract.timezone:
        return _seconds_until_anchor(contract.anchor, contract.timezone, now, interval)

    last = heartbeats.get(table_ref)
    if last is None:
        return interval
    elapsed = max(0, int((now - last).total_seconds()))
    remaining = interval - (elapsed % interval)
    return max(0, remaining)


def _seconds_until_anchor(anchor: str, tz_name: str, now: datetime, interval_seconds: int) -> int:
    """Round to the next ``HH:MM`` anchor in ``tz_name``.

    Stays robust for daily anchors. For sub-day intervals with an anchor,
    we walk forward in interval steps from the anchor of the current day
    until we land past ``now``.
    """
    from datetime import tzinfo
    from zoneinfo import ZoneInfo

    zone: tzinfo
    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        zone = UTC

    try:
        hh, mm = anchor.split(":")
        hour, minute = int(hh), int(mm)
    except Exception:
        hour, minute = 0, 0

    now_local = now.astimezone(zone)
    base = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if base > now_local:
        base = base - timedelta(days=1)
    candidate = base
    while candidate <= now_local:
        candidate = candidate + timedelta(seconds=interval_seconds)
    return max(0, int((candidate - now_local).total_seconds()))


def _max_staleness_remaining(
    contract: RefreshContract,
    table_ref: str,
    now: datetime,
    heartbeats: dict[str, datetime],
) -> int:
    """Heartbeat-mode TTL contribution.

    Without a recent heartbeat the table is considered already stale and
    yields zero. With a heartbeat at time ``H``, remaining = ``H +
    max_staleness - now``.
    """
    last = heartbeats.get(table_ref)
    if last is None:
        return 0
    max_stale = contract.max_staleness_seconds or 0
    if max_stale <= 0:
        return 0
    elapsed = int((now - last).total_seconds())
    return max(0, max_stale - elapsed)


def compute_effective_ttl(
    physical_tables: list[str],
    *,
    contracts: dict[str, RefreshContract],
    heartbeats: dict[str, datetime],
    caller_ttl_seconds: int | None = None,
    now: datetime | None = None,
    min_ttl_seconds: int = 5,
    max_ttl_seconds: int = 86400,
    unknown_policy: str = "no_cache",
    unknown_default_ttl_seconds: int = 300,
) -> TtlResult:
    """Compose per-table contracts into a single effective TTL.

    See PLAN §8. Returns :class:`TtlResult` with either a populated ``ttl``
    (cacheable) or a populated ``no_cache_reason`` (skip the cache).
    """
    now = now or datetime.now(UTC)

    contributions: list[tuple[int, str, str]] = []
    for table in physical_tables:
        contract = contracts.get(table) or RefreshContract(mode=RefreshMode.UNKNOWN)
        if contract.mode == RefreshMode.STATIC:
            continue
        if contract.mode == RefreshMode.INTERVAL:
            secs = _seconds_until_next_refresh(contract, table, now, heartbeats)
            contributions.append((secs, table, "interval"))
            continue
        if contract.mode == RefreshMode.HEARTBEAT:
            secs = _max_staleness_remaining(contract, table, now, heartbeats)
            contributions.append((secs, table, "heartbeat"))
            continue
        # UNKNOWN
        if unknown_policy == "default_ttl":
            contributions.append((unknown_default_ttl_seconds, table, "default_unknown"))
        else:
            return TtlResult(
                ttl=None,
                no_cache_reason=NoCacheReason.UNKNOWN_FRESHNESS,
                no_cache_table=table,
            )

    if not contributions:
        derived = max_ttl_seconds
        limiting_table = None
        limiting_reason = None
        source = "all_static"
    else:
        contributions.sort(key=lambda x: x[0])
        derived, limiting_table, limiting_reason = contributions[0]
        source = "freshness_derived"
        if limiting_reason == "default_unknown":
            source = "default_unknown"

    if derived <= 0:
        return TtlResult(
            ttl=None,
            no_cache_reason=NoCacheReason.ZERO_DERIVED_TTL,
            no_cache_table=limiting_table,
        )

    derived = min(derived, max_ttl_seconds)

    if caller_ttl_seconds is not None:
        if caller_ttl_seconds < derived:
            effective = caller_ttl_seconds
            source = "caller_capped"
        else:
            effective = derived
    else:
        effective = derived

    if effective < min_ttl_seconds:
        return TtlResult(
            ttl=None,
            no_cache_reason=NoCacheReason.BELOW_MIN_TTL,
            no_cache_table=limiting_table,
        )

    return TtlResult(
        ttl=TtlComputation(
            seconds=effective,
            derived_seconds=derived,
            caller_seconds=caller_ttl_seconds,
            source=source,
            limiting_table=limiting_table,
            limiting_reason=limiting_reason,
        )
    )
