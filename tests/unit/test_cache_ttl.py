"""Tests for cache.ttl — duration parsing + TTL composition."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orionbelt.cache.ttl import (
    NoCacheReason,
    RefreshContract,
    RefreshMode,
    compose_contracts,
    compute_effective_ttl,
    is_stricter,
    parse_duration,
)


class TestParseDuration:
    def test_seconds(self) -> None:
        assert parse_duration("30s") == 30

    def test_minutes(self) -> None:
        assert parse_duration("15m") == 900

    def test_hours(self) -> None:
        assert parse_duration("1h") == 3600

    def test_days(self) -> None:
        assert parse_duration("2d") == 172800

    def test_iso_simple(self) -> None:
        assert parse_duration("PT1H") == 3600
        assert parse_duration("PT30M") == 1800
        assert parse_duration("P1D") == 86400

    def test_iso_compound(self) -> None:
        assert parse_duration("PT1H30M") == 5400

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_duration("forever")
        with pytest.raises(ValueError):
            parse_duration("0s")
        with pytest.raises(ValueError):
            parse_duration("-1m")


class TestStrictness:
    def test_unknown_strictest(self) -> None:
        unknown = RefreshContract(mode=RefreshMode.UNKNOWN)
        static = RefreshContract(mode=RefreshMode.STATIC)
        assert is_stricter(unknown, static)
        assert not is_stricter(static, unknown)

    def test_shorter_interval_wins(self) -> None:
        a = RefreshContract(mode=RefreshMode.INTERVAL, interval_seconds=300)
        b = RefreshContract(mode=RefreshMode.INTERVAL, interval_seconds=3600)
        assert is_stricter(a, b)
        assert not is_stricter(b, a)

    def test_compose_picks_strictest(self) -> None:
        a = RefreshContract(mode=RefreshMode.INTERVAL, interval_seconds=3600)
        b = RefreshContract(mode=RefreshMode.HEARTBEAT, max_staleness_seconds=300)
        result = compose_contracts([a, b])
        assert result.mode == RefreshMode.HEARTBEAT


class TestComputeEffectiveTtl:
    def _now(self) -> datetime:
        return datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)

    def test_static_only_returns_max_ttl(self) -> None:
        contracts = {"WH.PUB.LOOKUP": RefreshContract(mode=RefreshMode.STATIC)}
        out = compute_effective_ttl(
            ["WH.PUB.LOOKUP"], contracts=contracts, heartbeats={}, now=self._now()
        )
        assert out.ttl is not None
        assert out.ttl.source == "all_static"
        assert out.ttl.seconds > 0

    def test_interval_no_heartbeat_uses_full_window(self) -> None:
        contracts = {
            "WH.PUB.ORDERS": RefreshContract(mode=RefreshMode.INTERVAL, interval_seconds=3600)
        }
        out = compute_effective_ttl(
            ["WH.PUB.ORDERS"], contracts=contracts, heartbeats={}, now=self._now()
        )
        assert out.ttl is not None
        assert out.ttl.seconds == 3600
        assert out.ttl.limiting_table == "WH.PUB.ORDERS"

    def test_min_takes_precedence_across_tables(self) -> None:
        contracts = {
            "WH.PUB.A": RefreshContract(mode=RefreshMode.INTERVAL, interval_seconds=600),
            "WH.PUB.B": RefreshContract(mode=RefreshMode.INTERVAL, interval_seconds=60),
        }
        out = compute_effective_ttl(
            ["WH.PUB.A", "WH.PUB.B"],
            contracts=contracts,
            heartbeats={},
            now=self._now(),
        )
        assert out.ttl is not None
        assert out.ttl.seconds == 60
        assert out.ttl.limiting_table == "WH.PUB.B"

    def test_heartbeat_recent_yields_remaining(self) -> None:
        now = self._now()
        contracts = {
            "WH.PUB.ORDERS": RefreshContract(mode=RefreshMode.HEARTBEAT, max_staleness_seconds=300)
        }
        heartbeats = {"WH.PUB.ORDERS": now - timedelta(seconds=60)}
        out = compute_effective_ttl(
            ["WH.PUB.ORDERS"], contracts=contracts, heartbeats=heartbeats, now=now
        )
        assert out.ttl is not None
        assert out.ttl.seconds == 240

    def test_heartbeat_missing_skips_cache(self) -> None:
        contracts = {
            "WH.PUB.ORDERS": RefreshContract(mode=RefreshMode.HEARTBEAT, max_staleness_seconds=300)
        }
        out = compute_effective_ttl(
            ["WH.PUB.ORDERS"], contracts=contracts, heartbeats={}, now=self._now()
        )
        assert out.ttl is None
        assert out.no_cache_reason == NoCacheReason.ZERO_DERIVED_TTL

    def test_unknown_no_cache_policy(self) -> None:
        out = compute_effective_ttl(
            ["WH.PUB.UNKNOWN"], contracts={}, heartbeats={}, now=self._now()
        )
        assert out.ttl is None
        assert out.no_cache_reason == NoCacheReason.UNKNOWN_FRESHNESS
        assert out.no_cache_table == "WH.PUB.UNKNOWN"

    def test_unknown_default_ttl_policy(self) -> None:
        out = compute_effective_ttl(
            ["WH.PUB.UNKNOWN"],
            contracts={},
            heartbeats={},
            now=self._now(),
            unknown_policy="default_ttl",
            unknown_default_ttl_seconds=200,
        )
        assert out.ttl is not None
        assert out.ttl.source == "default_unknown"
        assert out.ttl.seconds == 200

    def test_caller_caps_below_derived(self) -> None:
        contracts = {"WH.PUB.A": RefreshContract(mode=RefreshMode.INTERVAL, interval_seconds=3600)}
        out = compute_effective_ttl(
            ["WH.PUB.A"],
            contracts=contracts,
            heartbeats={},
            now=self._now(),
            caller_ttl_seconds=120,
        )
        assert out.ttl is not None
        assert out.ttl.seconds == 120
        assert out.ttl.source == "caller_capped"

    def test_caller_above_derived_uses_derived(self) -> None:
        contracts = {"WH.PUB.A": RefreshContract(mode=RefreshMode.INTERVAL, interval_seconds=120)}
        out = compute_effective_ttl(
            ["WH.PUB.A"],
            contracts=contracts,
            heartbeats={},
            now=self._now(),
            caller_ttl_seconds=99999,
        )
        assert out.ttl is not None
        assert out.ttl.seconds == 120
        assert out.ttl.source == "freshness_derived"

    def test_below_min_ttl_skips_cache(self) -> None:
        now = self._now()
        contracts = {
            "WH.PUB.ORDERS": RefreshContract(mode=RefreshMode.HEARTBEAT, max_staleness_seconds=10)
        }
        # remaining = 10 - 8 = 2s, below the 5s floor → no_cache
        heartbeats = {"WH.PUB.ORDERS": now - timedelta(seconds=8)}
        out = compute_effective_ttl(
            ["WH.PUB.ORDERS"],
            contracts=contracts,
            heartbeats=heartbeats,
            now=now,
            min_ttl_seconds=5,
        )
        assert out.ttl is None
        assert out.no_cache_reason == NoCacheReason.BELOW_MIN_TTL
