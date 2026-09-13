"""The Business Rules tab's callbacks: listing, testing one rule, the report, and errors."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("gradio", reason="gradio required for the UI handlers")
pytest.importorskip("pandas", reason="pandas required for the results table")

from orionbelt.ui import api_client  # noqa: E402
from orionbelt.ui.handlers import evaluate_all_rules_ui, evaluate_rule_ui, load_rules  # noqa: E402

_MODEL = "version: 1.0\ndataObjects: {}\n"


def _visible(update: object) -> bool:
    assert isinstance(update, dict)
    return bool(update.get("visible"))


def _client(status_code: int, body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    resp.text = str(body)
    client = MagicMock()
    client.request.return_value = resp
    return client


def _with(client: MagicMock):
    return patch.object(
        api_client,
        "_ensure_session_and_model",
        return_value=(client, "s1", "m1", {"session_id": "s1"}, {"model_id": "m1"}),
    )


_LISTING = {
    "dialect": "duckdb",
    "rules": [
        {
            "name": "High Return Rate",
            "type": "classification",
            "level": "aggregate",
            "findings": "matches",
            "severity": None,
            "grain": ["Product Category"],
            "measures": ["Return Rate"],
            "depends_on": [],
            "executable": True,
            "error": None,
        },
        {
            "name": "Broken",
            "type": "validation",
            "level": "aggregate",
            "findings": "violations",
            "severity": "error",
            "grain": ["X"],
            "measures": ["Y"],
            "depends_on": [],
            "executable": False,
            "error": "Unknown dimension X",
        },
    ],
    "statistics": {
        "total": 2,
        "by_type": {"classification": 1, "validation": 1},
        "by_level": {"aggregate": 2},
        "by_severity": {"error": 1},
        "executable": 1,
        "not_executable": 1,
    },
}


class TestLoad:
    def test_lists_rules_with_statistics(self) -> None:
        client = _client(200, _LISTING)
        with _with(client):
            stats, table, picker, session, model = load_rules(
                _MODEL, "http://api", "duckdb", None, None
            )
        client.request.assert_called_once_with(
            "GET", "/v1/sessions/s1/models/m1/rules?dialect=duckdb", json=None, timeout=300
        )
        assert stats.startswith("**2 rules** on `duckdb`")
        assert "executable 1, not executable 1" in stats
        assert _visible(table)
        frame = table["value"]
        assert list(frame["rule"]) == ["High Return Rate", "Broken"]
        assert frame.iloc[1]["executable"] == "no: Unknown dimension X"
        assert (
            picker["choices"] == ["High Return Rate", "Broken"]
            and picker["value"] == "High Return Rate"
        )
        assert session == {"session_id": "s1"} and model == {"model_id": "m1"}

    def test_no_rules(self) -> None:
        with _with(_client(200, {"dialect": "duckdb", "rules": [], "statistics": {"total": 0}})):
            stats, table, picker, _, _ = load_rules(_MODEL, "http://api", "", None, None)
        assert "declares no rules" in stats
        assert not _visible(table) and picker["choices"] == []

    def test_api_error_is_shown(self) -> None:
        with _with(_client(404, {"detail": "Model 'm1' not found"})):
            stats, table, _, _, _ = load_rules(_MODEL, "http://api", "", None, None)
        assert stats.startswith("**Error:**") and "not found" in stats
        assert not _visible(table)

    def test_placeholder_editor(self) -> None:
        stats, table, _, _, _ = load_rules("# API unreachable\n", "http://api", "", None, None)
        assert stats == "**Error:** No model loaded."


class TestTestRule:
    def test_findings_render(self) -> None:
        body = {
            "name": "High Return Rate",
            "type": "classification",
            "level": "aggregate",
            "findings": "matches",
            "severity": None,
            "columns": [{"name": "Product Category"}, {"name": "Return Rate"}],
            "rows": [["Toys", "12.5%"]],
            "row_count": 1,
            "execution_time_ms": 12.4,
            "cached": False,
        }
        client = _client(200, body)
        with _with(client):
            status, table, _, _ = evaluate_rule_ui(
                _MODEL, "http://api", "duckdb", "High Return Rate", None, None
            )
        client.request.assert_called_once_with(
            "POST",
            "/v1/sessions/s1/models/m1/rules/High Return Rate/evaluate",
            json={"format_values": True, "dialect": "duckdb"},
            timeout=300,
        )
        assert status == "**High Return Rate**: 1 matches (classification, aggregate) in 12 ms"
        assert _visible(table) and list(table["value"].columns) == [
            "Product Category",
            "Return Rate",
        ]

    def test_no_findings_hides_the_table(self) -> None:
        body = {
            "name": "Non-Negative Margin",
            "type": "validation",
            "level": "aggregate",
            "findings": "violations",
            "severity": "error",
            "columns": [{"name": "Product Category"}, {"name": "Gross Margin"}],
            "rows": [],
            "row_count": 0,
            "execution_time_ms": 3.0,
            "cached": True,
        }
        with _with(_client(200, body)):
            status, table, _, _ = evaluate_rule_ui(
                _MODEL, "http://api", "", "Non-Negative Margin", None, None
            )
        assert status.startswith(
            "**Non-Negative Margin**: 0 violations (validation, aggregate, severity error)"
        )
        assert status.endswith("(cached)")
        assert not _visible(table)

    def test_execution_disabled_is_shown(self) -> None:
        with _with(_client(503, {"detail": "Query execution is not available."})):
            status, table, _, _ = evaluate_rule_ui(_MODEL, "http://api", "", "R", None, None)
        assert status.startswith("**Error:**") and "not available" in status

    def test_no_rule_picked(self) -> None:
        status, table, _, _ = evaluate_rule_ui(_MODEL, "http://api", "", None, None, None)
        assert status == "Pick a rule to test." and not _visible(table)


class TestTestAll:
    def test_report_renders(self) -> None:
        body = {
            "dialect": "duckdb",
            "elapsed_ms": 40.0,
            "summary": {"total": 2, "executed": 1, "with_findings": 1, "failed": 1, "skipped": 0},
            "results": [
                {
                    "name": "A",
                    "type": "classification",
                    "findings": "matches",
                    "status": "executed",
                    "finding_count": 3,
                    "severity": None,
                    "cached": True,
                    "elapsed_ms": 10.2,
                    "error": None,
                },
                {
                    "name": "B",
                    "type": "validation",
                    "findings": "violations",
                    "status": "failed",
                    "finding_count": None,
                    "severity": "error",
                    "cached": False,
                    "elapsed_ms": 1.0,
                    "error": "boom",
                },
            ],
        }
        client = _client(200, body)
        with _with(client):
            text, table, _, _ = evaluate_all_rules_ui(_MODEL, "http://api", "duckdb", None, None)
        client.request.assert_called_once_with(
            "POST",
            "/v1/sessions/s1/models/m1/rules/evaluate",
            json={"limit": 20, "include_rows": False, "format_values": True, "dialect": "duckdb"},
            timeout=300,
        )
        assert text.startswith(
            "**Report** (duckdb, 40 ms): 2 rules, 1 executed, 1 with findings, 1 failed"
        )
        frame = table["value"]
        assert list(frame["status"]) == ["executed", "failed"]
        assert list(frame["count"]) == [3, ""]
        assert list(frame["error"]) == ["", "boom"]

    def test_unreachable_api(self) -> None:
        import httpx

        with patch.object(
            api_client, "_ensure_session_and_model", side_effect=httpx.ConnectError("x")
        ):
            text, table, _, _ = evaluate_all_rules_ui(_MODEL, "http://nope:1", "", None, None)
        assert text == "**Error:** API unreachable at http://nope:1."
        assert not _visible(table)
