"""The gap log: what it keeps of a refused query, and who reads it back."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from orionbelt.auth import (
    FULL_SCOPES,
    Principal,
    bind_principal,
    caller_key,
    current_caller,
    reset_principal,
)
from orionbelt.auth.principal import ANONYMOUS, API_KEY_PRINCIPAL
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.dialect.base import UnsupportedAggregationError
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import QueryObject
from orionbelt.service import gap_log, metrics
from orionbelt.service.model_store import ModelStore
from tests.conftest import SAMPLE_MODEL_YAML

USER = Principal(kind="user", sub="ada", scopes=frozenset({"obsl:read"}))
ADMIN = Principal(kind="user", sub="grace", scopes=FULL_SCOPES)


@pytest.fixture(autouse=True)
def _names_mode() -> Iterator[None]:
    gap_log.configure(gap_log.GAP_NAMES, 100)
    yield
    gap_log.configure(gap_log.GAP_OFF, 1)


def _unknown_measure(name: str = "Total Revnue") -> ResolutionError:
    return ResolutionError(
        [
            SemanticError(
                code="UNKNOWN_MEASURE",
                message=f"Unknown measure '{name}'",
                suggestions=["Total Revenue"],
                context={"measure": name},
            )
        ]
    )


def _query(**where: str) -> QueryObject:
    return QueryObject.model_validate(
        {
            "select": {"dimensions": ["Customer Country"], "measures": ["Total Revnue"]},
            "where": [
                {"field": field, "op": "equals", "value": value} for field, value in where.items()
            ],
        }
    )


def _record_as(principal: Principal | None, exc: Exception, *, session_id: str | None = None):
    binding = bind_principal(principal, session_id=session_id) if principal else None
    try:
        gap_log.record_refusal("m1", exc, _query())
        return current_caller()
    finally:
        if binding is not None:
            reset_principal(binding)


class TestCallerKey:
    def test_each_kind_of_caller_gets_a_short_key(self) -> None:
        for principal, session in (
            (USER, None),
            (Principal(kind="api_key", key_id="ab12cd34", scopes=FULL_SCOPES), None),
            (ANONYMOUS, "s-1"),
        ):
            key = caller_key(principal, session)
            assert key is not None
            assert re.fullmatch(r"[0-9a-f]{12}", key)

    def test_no_identity_and_no_session_has_no_key(self) -> None:
        assert caller_key(ANONYMOUS) is None
        assert caller_key(API_KEY_PRINCIPAL) is None  # a key always carries its id

    def test_an_identity_wins_over_the_session(self) -> None:
        assert caller_key(USER, "s-1") == caller_key(USER, "s-2")

    def test_kinds_never_share_a_key(self) -> None:
        user = Principal(kind="user", sub="same", scopes=FULL_SCOPES)
        key = Principal(kind="api_key", key_id="same", scopes=FULL_SCOPES)
        assert len({caller_key(user), caller_key(key), caller_key(ANONYMOUS, "same")}) == 3

    def test_the_key_is_bound_with_the_principal(self) -> None:
        binding = bind_principal(ANONYMOUS, session_id="s-1")
        try:
            assert current_caller() == caller_key(ANONYMOUS, "s-1")
        finally:
            reset_principal(binding)
        assert current_caller() is None


class TestConfigure:
    def test_an_unknown_mode_is_refused(self) -> None:
        with pytest.raises(ValueError, match="GAP_LOG must be one of off, names, full"):
            gap_log.configure("all", 10)

    def test_a_size_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="GAP_LOG_SIZE must be at least 1"):
            gap_log.configure(gap_log.GAP_NAMES, 0)

    def test_off_keeps_nothing(self) -> None:
        gap_log.configure(gap_log.GAP_OFF, 10)
        _record_as(USER, _unknown_measure())
        assert gap_log.current_mode() == "off"
        assert gap_log.read("m1", caller=caller_key(USER), see_all=True) == []


class TestWhatIsKept:
    def test_names_keeps_names_and_codes_only(self) -> None:
        token = metrics.SURFACE.set("pgwire")
        try:
            caller = _record_as(USER, _unknown_measure())
        finally:
            metrics.SURFACE.reset(token)
        [record] = gap_log.read("m1", caller=caller, see_all=False)
        assert record.code == "UNKNOWN_MEASURE"
        assert record.subject == "Total Revnue"
        assert record.suggestions == ("Total Revenue",)
        assert record.dimensions == ("Customer Country",)
        assert record.measures == ("Total Revnue",)
        assert record.surface == "pgwire"
        assert record.message is None
        assert record.filters == ()

    def test_full_also_keeps_the_message_and_filters(self) -> None:
        gap_log.configure(gap_log.GAP_FULL, 10)
        binding = bind_principal(USER)
        try:
            gap_log.record_refusal("m1", _unknown_measure(), _query(**{"Customer Country": "DE"}))
        finally:
            reset_principal(binding)
        [record] = gap_log.read("m1", caller=caller_key(USER), see_all=False)
        assert record.message == "Unknown measure 'Total Revnue'"
        assert record.filters == ({"field": "Customer Country", "op": "equals", "value": "DE"},)

    def test_the_subject_comes_only_from_a_key_that_holds_a_name(self) -> None:
        error = SemanticError(code="BAD_VALUE", message="x", context={"value": "jane@acme.com"})
        caller = _record_as(USER, ResolutionError([error]))
        [record] = gap_log.read("m1", caller=caller, see_all=False)
        assert record.subject is None

    def test_a_dialect_refusal_gets_the_code_the_api_reports(self) -> None:
        caller = _record_as(USER, UnsupportedAggregationError("mysql", "median"))
        [record] = gap_log.read("m1", caller=caller, see_all=False)
        assert (record.code, record.subject) == ("UNSUPPORTED_AGGREGATION", "median")

    def test_one_record_per_error(self) -> None:
        exc = ResolutionError([*_unknown_measure("A").errors, *_unknown_measure("B").errors])
        caller = _record_as(USER, exc)
        assert [r.subject for r in gap_log.read("m1", caller=caller, see_all=False)] == ["A", "B"]

    def test_the_oldest_records_go_first(self) -> None:
        gap_log.configure(gap_log.GAP_NAMES, 2)
        for name in ("A", "B", "C"):
            caller = _record_as(USER, _unknown_measure(name))
        assert [r.subject for r in gap_log.read("m1", caller=caller, see_all=False)] == ["B", "C"]

    def test_recording_never_fails_the_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def broken(_records: object) -> None:
            raise RuntimeError("disk full")

        monkeypatch.setattr(gap_log._log, "add", broken)
        gap_log.record_refusal("m1", _unknown_measure())  # logged, not raised


class TestWhoReads:
    def test_a_caller_reads_only_its_own_records(self) -> None:
        mine = _record_as(USER, _unknown_measure("Mine"))
        _record_as(ADMIN, _unknown_measure("Theirs"))
        assert [r.subject for r in gap_log.read("m1", caller=mine, see_all=False)] == ["Mine"]

    def test_a_record_without_a_caller_is_read_by_no_caller(self) -> None:
        _record_as(None, _unknown_measure())
        assert gap_log.read("m1", caller=None, see_all=False) == []

    def test_reading_all_returns_every_callers_records(self) -> None:
        _record_as(USER, _unknown_measure("Mine"))
        _record_as(None, _unknown_measure("Nobody's"))
        assert len(gap_log.read("m1", caller=None, see_all=True)) == 2

    def test_only_a_signed_in_admin_reads_all(self) -> None:
        assert gap_log.may_read_all(ADMIN)
        assert not gap_log.may_read_all(USER)
        assert not gap_log.may_read_all(Principal(kind="api_key", key_id="k", scopes=FULL_SCOPES))
        assert not gap_log.may_read_all(ANONYMOUS)
        assert not gap_log.may_read_all(None)

    def test_records_are_kept_per_model(self) -> None:
        caller = _record_as(USER, _unknown_measure())
        assert gap_log.read("other", caller=caller, see_all=True) == []

    def test_code_and_since_narrow_the_records(self) -> None:
        caller = _record_as(USER, _unknown_measure())
        _record_as(USER, UnsupportedAggregationError("mysql", "median"))
        codes = [r.code for r in gap_log.read("m1", caller=caller, see_all=False, code="FANOUT")]
        assert codes == []
        later = datetime.now(UTC) + timedelta(seconds=1)
        assert gap_log.read("m1", caller=caller, see_all=False, since=later) == []


class TestSummarize:
    def test_one_row_per_code_and_subject_the_most_frequent_first(self) -> None:
        for name in ("A", "B", "B"):
            _record_as(USER, _unknown_measure(name))
        _record_as(ADMIN, _unknown_measure("B"))
        rows = gap_log.summarize(gap_log.read("m1", caller=None, see_all=True))
        assert [(r.subject, r.count, r.callers) for r in rows] == [("B", 3, 2), ("A", 1, 1)]
        assert rows[0].first_seen <= rows[0].last_seen


class TestModelStore:
    def test_a_refused_compile_is_recorded_and_still_raised(self) -> None:
        store = ModelStore()
        model_id = store.load_model(SAMPLE_MODEL_YAML).model_id
        binding = bind_principal(USER)
        try:
            with pytest.raises(ResolutionError):
                store.compile_query(model_id, _query(), "postgres")
        finally:
            reset_principal(binding)
        [record] = gap_log.read(model_id, caller=caller_key(USER), see_all=False)
        assert (record.code, record.subject) == ("UNKNOWN_MEASURE", "Total Revnue")
        assert "Total Revenue" in record.suggestions
