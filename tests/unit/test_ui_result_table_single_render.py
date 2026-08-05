"""The result table is written once per run, not twice.

``execute_query`` used to return a bare DataFrame for the result table, and
the caller revealed the table in a chained ``.then`` that sent a second update
to the same component. Gradio renders each update, so every run and every sort
click painted the Dataframe twice, which read as a flicker. The visibility now
rides along with the value, so the table is one output of one call.
"""

from __future__ import annotations

import pytest

pytest.importorskip("gradio", reason="gradio required by the UI handlers")

from orionbelt.ui.handlers import execute_query  # noqa: E402

# Index of the result-table slot in the execute_query tuple.
_TABLE = 4
# Index of result_info, the emptiness of which decides the table's visibility.
_INFO = 6


def _run(query_yaml: str) -> tuple:
    return execute_query("version: 1.0", query_yaml, "duckdb", "http://unused", None, None)


def test_a_failed_run_hides_the_table_in_its_own_update() -> None:
    """No second round-trip is needed to hide it: the value update carries it."""
    result = _run("this: [is not valid yaml")

    assert result[_INFO] == ""
    table = result[_TABLE]
    assert isinstance(table, dict), "the table slot must be a gr.update, not a bare frame"
    assert table["visible"] is False


def test_the_table_update_still_carries_the_value() -> None:
    """Folding visibility in must not drop the data the table renders."""
    table = _run("this: [is not valid yaml")[_TABLE]

    assert "value" in table, "the update must carry the frame, or the table renders empty"


def test_visibility_tracks_result_info() -> None:
    """The rule the removed .then applied (`visible=bool(info)`) is preserved.

    Asserted against the same handler output rather than restated, so the two
    cannot drift apart the way the value and its visibility did. Driven off an
    error path on purpose: the success path needs a live API, and this
    invariant holds either way.
    """
    result = _run("version: 1.0")  # a mapping, but not a query

    assert result[_TABLE]["visible"] is bool(result[_INFO])
