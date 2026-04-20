"""Tests for timezone resolution and temporal value serialization."""

from __future__ import annotations

from datetime import datetime, time
from datetime import timezone as tz_mod
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from orionbelt.models.semantic import ModelSettings
from orionbelt.service.db_executor import (
    _detect_db_timezone,
    _reset_db_tz_cache,
    _serialize_value,
    resolve_timezone,
)


class TestModelSettingsTimezone:
    def test_valid_timezone(self) -> None:
        s = ModelSettings(default_timezone="Europe/Zagreb")
        assert s.default_timezone == "Europe/Zagreb"

    def test_valid_utc(self) -> None:
        s = ModelSettings(default_timezone="UTC")
        assert s.default_timezone == "UTC"

    def test_invalid_timezone(self) -> None:
        with pytest.raises(ValueError, match="valid IANA timezone"):
            ModelSettings(default_timezone="NotATimezone/Nowhere")

    def test_allow_utc_fallback_default(self) -> None:
        s = ModelSettings()
        assert s.allow_utc_fallback is False

    def test_allow_utc_fallback_true(self) -> None:
        s = ModelSettings(allow_utc_fallback=True)
        assert s.allow_utc_fallback is True

    def test_combined_settings(self) -> None:
        s = ModelSettings(
            default_numeric_data_type="decimal(18, 4)",
            default_timezone="America/New_York",
            allow_utc_fallback=True,
        )
        assert s.default_numeric_data_type == "decimal(18, 4)"
        assert s.default_timezone == "America/New_York"
        assert s.allow_utc_fallback is True


class TestResolveTimezone:
    def test_model_timezone_wins(self) -> None:
        result = resolve_timezone(default_timezone="Europe/Zagreb")
        assert result == ZoneInfo("Europe/Zagreb")

    def test_utc_fallback_when_enabled(self) -> None:
        result = resolve_timezone(allow_utc_fallback=True)
        # Either host TZ or UTC
        assert result is not None

    def test_no_fallback_returns_none_or_host(self) -> None:
        result = resolve_timezone()
        # On CI or containers without TZ, this may be None
        # On dev machines with TZ set, this is the host TZ

    def test_invalid_timezone_falls_through(self) -> None:
        result = resolve_timezone(
            default_timezone="Invalid/Zone", allow_utc_fallback=True
        )
        # Falls through to host or UTC fallback
        assert result is not None


class TestSerializeTemporalValues:
    def test_date_iso(self) -> None:
        from datetime import date

        assert _serialize_value(date(2026, 4, 19)) == "2026-04-19"

    def test_time_iso(self) -> None:
        assert _serialize_value(time(14, 30, 0)) == "14:30:00"

    def test_time_with_microseconds(self) -> None:
        assert _serialize_value(time(14, 30, 0, 123456)) == "14:30:00.123456"

    def test_time_zero_microseconds_elided(self) -> None:
        assert _serialize_value(time(14, 30, 0, 0)) == "14:30:00"

    def test_datetime_tz_aware_preserves_offset(self) -> None:
        dt = datetime(2026, 4, 19, 14, 30, 0, tzinfo=ZoneInfo("Europe/Zagreb"))
        result = _serialize_value(dt)
        assert "+02:00" in result or "+01:00" in result

    def test_datetime_utc_uses_z(self) -> None:
        dt = datetime(2026, 4, 19, 14, 30, 0, tzinfo=tz_mod.utc)
        result = _serialize_value(dt)
        assert result.endswith("Z")

    def test_datetime_naive_no_tz_passes_through(self) -> None:
        dt = datetime(2026, 4, 19, 14, 30, 0)
        result = _serialize_value(dt, tz=None)
        assert result == "2026-04-19T14:30:00"

    def test_datetime_naive_with_tz_applies(self) -> None:
        dt = datetime(2026, 4, 19, 14, 30, 0)
        tz = ZoneInfo("Europe/Zagreb")
        result = _serialize_value(dt, tz=tz)
        assert "+02:00" in result or "+01:00" in result
        assert "2026-04-19T14:30:00" in result

    def test_datetime_aware_ignores_passed_tz(self) -> None:
        dt = datetime(2026, 4, 19, 14, 30, 0, tzinfo=tz_mod.utc)
        tz = ZoneInfo("Europe/Zagreb")
        result = _serialize_value(dt, tz=tz)
        # Already tz-aware — don't override
        assert result.endswith("Z")


class TestParserSettings:
    def test_settings_roundtrip(self) -> None:
        from orionbelt.parser import ReferenceResolver, TrackedLoader

        yaml = """
version: "1.0"
settings:
  defaultTimezone: "Europe/Zagreb"
  allowUtcFallback: true
dataObjects:
  T:
    code: T
    columns:
      A: { code: A, abstractType: float }
dimensions:
  Dim:
    dataObject: T
    column: A
    resultType: string
measures:
  Total:
    resultType: float
    aggregation: sum
    expression: "{[T].[A]}"
"""
        loader = TrackedLoader()
        raw, sm = loader.load_string(yaml)
        resolver = ReferenceResolver()
        model, _ = resolver.resolve(raw, sm)

        assert model.settings is not None
        assert model.settings.default_timezone == "Europe/Zagreb"
        assert model.settings.allow_utc_fallback is True


class TestDetectDbTimezone:
    """Tests for database session timezone detection and caching."""

    def setup_method(self) -> None:
        _reset_db_tz_cache()

    class MockCursor:
        """Mock cursor that returns a configurable timezone from execute()."""

        def __init__(self, tz_name: str | None = None) -> None:
            self._tz_name = tz_name

        def execute(self, sql: str) -> None:
            pass

        def fetchone(self) -> tuple[str, ...] | None:
            if self._tz_name is None:
                return None
            return (self._tz_name,)

    class MockDuckDBConn:
        """Mock DuckDB connection where execute() returns a result object."""

        def __init__(self, tz_name: str) -> None:
            self._tz_name = tz_name

        def execute(self, sql: str) -> Any:
            class Result:
                def __init__(self, val: str) -> None:
                    self._val = val

                def fetchone(self) -> tuple[str, ...]:
                    return (self._val,)

            return Result(self._tz_name)

    def test_detect_postgres(self) -> None:
        cursor = self.MockCursor("Europe/Berlin")
        result = _detect_db_timezone(cursor, "postgres")
        assert result == ZoneInfo("Europe/Berlin")

    def test_detect_snowflake(self) -> None:
        cursor = self.MockCursor("America/New_York")
        result = _detect_db_timezone(cursor, "snowflake")
        assert result == ZoneInfo("America/New_York")

    def test_detect_duckdb(self) -> None:
        conn = self.MockDuckDBConn("Europe/Zagreb")
        result = _detect_db_timezone(conn, "duckdb")
        assert result == ZoneInfo("Europe/Zagreb")

    def test_detect_clickhouse(self) -> None:
        cursor = self.MockCursor("Asia/Tokyo")
        result = _detect_db_timezone(cursor, "clickhouse")
        assert result == ZoneInfo("Asia/Tokyo")

    def test_detect_mysql(self) -> None:
        cursor = self.MockCursor("US/Eastern")
        result = _detect_db_timezone(cursor, "mysql")
        assert result == ZoneInfo("US/Eastern")

    def test_bigquery_always_utc(self) -> None:
        cursor = self.MockCursor(None)
        result = _detect_db_timezone(cursor, "bigquery")
        assert result == ZoneInfo("UTC")

    def test_unsupported_dialect_returns_none(self) -> None:
        cursor = self.MockCursor("UTC")
        result = _detect_db_timezone(cursor, "databricks")
        assert result is None

    def test_system_tz_returns_none(self) -> None:
        cursor = self.MockCursor("SYSTEM")
        result = _detect_db_timezone(cursor, "mysql")
        assert result is None

    def test_invalid_tz_name_returns_none(self) -> None:
        cursor = self.MockCursor("Not/A/Real/Zone")
        result = _detect_db_timezone(cursor, "postgres")
        assert result is None

    def test_caching_only_queries_once(self) -> None:
        call_count = 0

        class CountingCursor:
            def execute(self, sql: str) -> None:
                nonlocal call_count
                call_count += 1

            def fetchone(self) -> tuple[str, ...]:
                return ("Europe/Zagreb",)

        cursor = CountingCursor()
        result1 = _detect_db_timezone(cursor, "postgres")
        result2 = _detect_db_timezone(cursor, "postgres")
        assert result1 == ZoneInfo("Europe/Zagreb")
        assert result2 == ZoneInfo("Europe/Zagreb")
        assert call_count == 1

    def test_db_tz_takes_priority_over_model_tz(self) -> None:
        """DB session TZ should be used, not model defaultTimezone."""
        cursor = self.MockCursor("America/Chicago")
        db_tz = _detect_db_timezone(cursor, "postgres")
        model_tz = resolve_timezone(default_timezone="Europe/Zagreb")

        effective_tz = db_tz or model_tz
        assert effective_tz == ZoneInfo("America/Chicago")

    def test_model_tz_used_when_detection_fails(self) -> None:
        """Model TZ is fallback when DB detection fails."""
        cursor = self.MockCursor(None)
        db_tz = _detect_db_timezone(cursor, "postgres")
        model_tz = resolve_timezone(default_timezone="Europe/Zagreb")

        effective_tz = db_tz or model_tz
        assert effective_tz == ZoneInfo("Europe/Zagreb")

    def test_exception_during_detection_returns_none(self) -> None:
        class FailingCursor:
            def execute(self, sql: str) -> None:
                raise RuntimeError("connection lost")

        cursor = FailingCursor()
        result = _detect_db_timezone(cursor, "snowflake")
        assert result is None
