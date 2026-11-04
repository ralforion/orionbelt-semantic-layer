"""Tests for timezone resolution and temporal value serialization."""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from orionbelt.models.semantic import ModelSettings
from orionbelt.service import db_executor
from orionbelt.service.db_executor import (
    ExecutionUnavailableError,
    _detect_db_timezone,
    _reset_db_tz_cache,
    _serialize_value,
    declared_timezone,
    resolve_timezone,
    timezone_epoch,
    zone_key,
    zone_named,
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

    def test_override_database_timezone_default(self) -> None:
        s = ModelSettings()
        assert s.override_database_timezone is False

    def test_override_database_timezone_true(self) -> None:
        s = ModelSettings(override_database_timezone=True)
        assert s.override_database_timezone is True

    def test_default_dialect_accepts_registered_name(self) -> None:
        s = ModelSettings(default_dialect="postgres")
        assert s.default_dialect == "postgres"

    def test_default_dialect_alias_camel_case(self) -> None:
        # YAML key is ``defaultDialect``; Python alias path must work too.
        s = ModelSettings.model_validate({"defaultDialect": "snowflake"})
        assert s.default_dialect == "snowflake"

    def test_default_dialect_rejects_unknown(self) -> None:
        with pytest.raises(ValueError, match="defaultDialect must be one of"):
            ModelSettings(default_dialect="sqlserver")

    def test_default_dialect_optional(self) -> None:
        s = ModelSettings()
        assert s.default_dialect is None

    def test_combined_settings(self) -> None:
        s = ModelSettings(
            default_numeric_data_type="decimal(18, 4)",
            default_timezone="America/New_York",
            override_database_timezone=True,
        )
        assert s.default_numeric_data_type == "decimal(18, 4)"
        assert s.default_timezone == "America/New_York"
        assert s.override_database_timezone is True


class TestResolveTimezone:
    def test_model_timezone_wins(self) -> None:
        result = resolve_timezone(default_timezone="Europe/Zagreb")
        assert result == ZoneInfo("Europe/Zagreb")

    def test_always_returns_timezone(self) -> None:
        result = resolve_timezone()
        assert result is not None

    def test_invalid_timezone_falls_through_to_utc(self) -> None:
        result = resolve_timezone(default_timezone="Invalid/Zone")
        # Falls through to host TZ or UTC
        assert result is not None


class TestHostTimezone:
    """The server's zone, by the IANA name a database gives it."""

    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(db_executor, "_HOST_TZ", None)

    def test_named_by_tz(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TZ", "America/Chicago")
        assert db_executor._get_host_timezone() == ZoneInfo("America/Chicago")

    def test_utc_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TZ", "Etc/UTC")
        assert db_executor._get_host_timezone() is None

    def test_by_the_zone_localtime_links_to(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Not the clock's abbreviation, which names no zone in summer (CEST).
        monkeypatch.delenv("TZ", raising=False)
        monkeypatch.setattr(
            db_executor.os.path,
            "realpath",
            lambda _: "/var/db/timezone/zoneinfo/Europe/Berlin",
        )
        monkeypatch.setattr(db_executor, "_clock_zone_name", lambda: "CEST")
        assert db_executor._get_host_timezone() == ZoneInfo("Europe/Berlin")


class TestZoneNames:
    @pytest.mark.parametrize(
        ("name", "zone"),
        [
            ("Europe/Berlin", "Europe/Berlin"),
            ("+00:00", "UTC"),
            ("+02:00", "Etc/GMT-2"),
            ("-05:00", "Etc/GMT+5"),
        ],
    )
    def test_a_zone_or_a_whole_hour_offset(self, name: str, zone: str) -> None:
        assert zone_named(name) == ZoneInfo(zone)

    @pytest.mark.parametrize("name", ["", "+05:30", "CEST", "Mars/Olympus"])
    def test_none_names_no_zone(self, name: str) -> None:
        assert zone_named(name) is None

    def test_every_name_of_utc_is_utc(self) -> None:
        assert {zone_key(ZoneInfo(n)) for n in ("UTC", "Etc/UTC", "Zulu", "GMT")} == {"UTC"}
        assert zone_key(ZoneInfo("Europe/Berlin")) == "Europe/Berlin"


class TestDeclaredTimezone:
    @pytest.fixture
    def source(self) -> Any:
        from ob_flight.datasources import DataSource

        return DataSource(
            name="crm",
            dialect="postgres",
            prefix="DB_CRM_",
            environ={"DB_CRM_TIMEZONE": "America/New_York", "DB_TIMEZONE": "UTC"},
        )

    def test_a_source_s_own_before_the_shared_one(self, source: Any) -> None:
        assert declared_timezone(source) == ZoneInfo("America/New_York")

    def test_the_shared_one(self, source: Any) -> None:
        from dataclasses import replace

        assert declared_timezone(replace(source, environ={"DB_TIMEZONE": "UTC"})) == ZoneInfo("UTC")
        assert declared_timezone(replace(source, environ={})) is None

    def test_a_dialect_s_own_source_reads_the_shared_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DB_TIMEZONE", "Asia/Tokyo")
        assert declared_timezone("postgres") == ZoneInfo("Asia/Tokyo")

    def test_a_name_of_no_zone_is_refused(self, source: Any) -> None:
        from dataclasses import replace

        with pytest.raises(ExecutionUnavailableError, match="DB_CRM_TIMEZONE is 'CEST'"):
            declared_timezone(replace(source, environ={"DB_CRM_TIMEZONE": "CEST"}))


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
        dt = datetime(2026, 4, 19, 14, 30, 0, tzinfo=UTC)
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
        dt = datetime(2026, 4, 19, 14, 30, 0, tzinfo=UTC)
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
  overrideDatabaseTimezone: true
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
        assert model.settings.override_database_timezone is True


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

    class MySQLCursor:
        """``@@session.time_zone`` and ``@@system_time_zone``."""

        def __init__(self, session: str, system: str) -> None:
            self._row = (session, system)

        def execute(self, sql: str) -> None:
            assert "@@system_time_zone" in sql

        def fetchone(self) -> tuple[str, str]:
            return self._row

    def test_mysql_in_a_utc_system_zone(self) -> None:
        assert _detect_db_timezone(self.MySQLCursor("SYSTEM", "UTC"), "mysql") == ZoneInfo("UTC")

    def test_mysql_in_another_system_zone_is_not_known(self) -> None:
        # EST is a fixed offset; a server in New York keeps summer time.
        assert _detect_db_timezone(self.MySQLCursor("SYSTEM", "EST"), "mysql") is None

    def test_mysql_session_zone_before_the_system_one(self) -> None:
        cursor = self.MySQLCursor("Europe/Berlin", "UTC")
        assert _detect_db_timezone(cursor, "mysql") == ZoneInfo("Europe/Berlin")

    def test_mysql_offset(self) -> None:
        assert _detect_db_timezone(self.MockCursor("+02:00"), "mysql") == ZoneInfo("Etc/GMT-2")

    def test_a_zone_becoming_known_changes_the_epoch(self) -> None:
        before = timezone_epoch()
        _detect_db_timezone(self.MockCursor("Asia/Tokyo"), "postgres")
        known = timezone_epoch()
        _detect_db_timezone(self.MockCursor("Asia/Tokyo"), "postgres")
        assert before != known == timezone_epoch()

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

    def test_override_uses_model_tz_over_db_tz(self) -> None:
        """When override_db_tz=True, model TZ wins even if DB TZ detected."""
        cursor = self.MockCursor("America/Chicago")
        db_tz = _detect_db_timezone(cursor, "postgres")
        model_tz = resolve_timezone(default_timezone="Europe/Zagreb")

        # Simulate override logic from execute_sql
        override_db_tz = True
        effective_tz = model_tz if (override_db_tz and model_tz) else (db_tz or model_tz)
        assert effective_tz == ZoneInfo("Europe/Zagreb")

    def test_exception_during_detection_returns_none(self) -> None:
        class FailingCursor:
            def execute(self, sql: str) -> None:
                raise RuntimeError("connection lost")

        cursor = FailingCursor()
        result = _detect_db_timezone(cursor, "snowflake")
        assert result is None
