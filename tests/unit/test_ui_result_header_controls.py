"""The Query Results headers: a cycling sort icon, and a filter icon with a ✕.

The header JS reads two states the server writes after every run (the
``orderBy`` and the click-added filters) and sends ``column|action|nonce`` back,
which :func:`_apply_header_signal` turns into a rewritten query.
"""

from __future__ import annotations

import json

import pytest
import yaml

pytest.importorskip("gradio", reason="gradio required by the UI handlers")

from orionbelt.ui.handlers import (  # noqa: E402
    _apply_header_signal,
    filter_state_str,
    header_states,
    query_parse_problem,
    remove_cell_filter,
)

QUERY = """\
select:
  dimensions:
    - Product Name
    - Client Name
  measures:
    - Total Sales
where:
  - field: Country Name
    op: in
    value: [Germany, France, Italy]
  - field: Client Name
    op: equals
    value: Kai Garcia
  - field: Product Name
    op: notset
having:
  - field: Total Sales
    op: equals
    value: 100
orderBy:
  - field: Total Sales
    direction: desc
"""


def _where_fields(query: str) -> list[str]:
    return [w["field"] for w in yaml.safe_load(query).get("where") or []]


def test_filter_state_lists_each_click_filter_by_column() -> None:
    assert json.loads(filter_state_str(QUERY)) == {
        "Client Name": "Client Name = Kai Garcia",
        "Product Name": "Product Name IS NULL",
        "Total Sales": "Total Sales = 100",
    }


def test_filter_state_leaves_out_the_querys_own_filters() -> None:
    """``Country Name in [...]`` was written by hand: no ✕ to remove it."""
    assert "Country Name" not in json.loads(filter_state_str(QUERY))


def test_filter_state_of_a_query_without_click_filters_is_empty() -> None:
    assert json.loads(filter_state_str("select:\n  measures: [Total Sales]\n")) == {}
    assert json.loads(filter_state_str("select: [\n")) == {}  # does not parse


def test_filter_state_keeps_any_character_in_a_value() -> None:
    query = "where:\n  - field: Client Name\n    op: equals\n    value: 'a|b\\n\"c\"'\n"
    assert json.loads(filter_state_str(query)) == {"Client Name": 'Client Name = a|b\\n"c"'}


def test_removing_one_columns_filter_keeps_the_others() -> None:
    out = remove_cell_filter(QUERY, "Client Name")
    assert _where_fields(out) == ["Country Name", "Product Name"]
    assert yaml.safe_load(out)["having"] == [{"field": "Total Sales", "op": "equals", "value": 100}]


def test_removing_a_measure_filter_drops_an_emptied_having() -> None:
    out = remove_cell_filter(QUERY, "Total Sales")
    assert "having" not in yaml.safe_load(out)
    assert _where_fields(out) == ["Country Name", "Client Name", "Product Name"]


def test_removing_a_filter_never_touches_the_querys_own() -> None:
    assert remove_cell_filter(QUERY, "Country Name") == remove_cell_filter(QUERY, "Nobody")
    assert "Country Name" in _where_fields(remove_cell_filter(QUERY, "Country Name"))


def test_clear_all_still_removes_every_click_filter() -> None:
    out = remove_cell_filter(QUERY)
    assert _where_fields(out) == ["Country Name"]
    assert "having" not in yaml.safe_load(out)


@pytest.mark.parametrize(
    ("action", "order_by"),
    [
        (
            "asc",
            [
                {"field": "Total Sales", "direction": "desc"},
                {"field": "Client Name", "direction": "asc"},
            ],
        ),
        (
            "desc",
            [
                {"field": "Total Sales", "direction": "desc"},
                {"field": "Client Name", "direction": "desc"},
            ],
        ),
        ("clear", [{"field": "Total Sales", "direction": "desc"}]),
    ],
)
def test_the_sort_icon_cycles_a_column(action: str, order_by: list[dict[str, str]]) -> None:
    out = _apply_header_signal(QUERY, f"Client Name|{action}|1700000000")
    assert yaml.safe_load(out)["orderBy"] == order_by


def test_clearing_the_last_sort_drops_order_by() -> None:
    out = _apply_header_signal(QUERY, "Total Sales|clear|1")
    assert "orderBy" not in yaml.safe_load(out)


def test_the_x_beside_a_filter_icon_removes_that_filter() -> None:
    out = _apply_header_signal(QUERY, "Client Name|unfilter|1")
    assert _where_fields(out) == ["Country Name", "Product Name"]


@pytest.mark.parametrize("signal", ["", None, "|asc|1", "Client Name"])
def test_a_malformed_signal_leaves_the_query_alone(signal: str | None) -> None:
    assert _apply_header_signal(QUERY, signal) == QUERY


def test_header_states_after_a_run() -> None:
    sort_state, filter_state = header_states(QUERY)
    assert sort_state == "Total Sales|desc"
    assert set(json.loads(filter_state)) == {"Client Name", "Product Name", "Total Sales"}


def test_a_query_that_parses_has_no_parse_problem() -> None:
    assert query_parse_problem(QUERY) is None
    assert query_parse_problem("") is None


def test_a_query_that_does_not_parse_names_its_line() -> None:
    """ "measures:" one space deeper than "dimensions:" (the reported case)."""
    broken = QUERY.replace("\n  measures:", "\n   measures:")
    assert query_parse_problem(broken) == "line 5"
