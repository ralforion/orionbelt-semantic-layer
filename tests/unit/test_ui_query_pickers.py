"""The Dimensions, Measures / Metrics and Columns pickers write into the query YAML.

A pick adds to the key the query already has, at whatever indentation the editor
holds, and keeps the layout. A query that does not parse is left alone: the
text-matching insert this replaces looked for exactly ``"  measures:"`` and,
failing to find it, appended a second ``measures:`` block.
"""

from __future__ import annotations

import pytest
import yaml

pytest.importorskip("gradio", reason="gradio required by the UI handlers")

from orionbelt.ui.handlers import QueryInsertError, _insert_into_query  # noqa: E402

# The playground's starter query (``_DEFAULT_QUERY``): list items four spaces in.
STARTER = """\
select:
  dimensions:
    - Product Name
    - Client Name
  measures:
    - Total Sales
    - Total Returns
    - Return Rate
where:
  - field: Country Name
    op: in
    value: [Germany, France, Italy]
orderBy:
  - field: Total Sales
    direction: desc
limit: 100
"""

# The layout a click-to-filter or sort in Query Results writes: dashes at the
# key's own indentation.
REWRITTEN = """\
select:
  dimensions:
  - Product Name
  measures:
  - Total Sales
limit: 100
"""


def test_a_measure_goes_under_the_existing_key() -> None:
    out = _insert_into_query(STARTER, "Average Sale", "measures")
    assert out == STARTER.replace("    - Return Rate\n", "    - Return Rate\n    - Average Sale\n")


def test_a_dimension_goes_under_the_existing_key() -> None:
    out = _insert_into_query(STARTER, "Client Gender", "dimensions")
    assert out == STARTER.replace("    - Client Name\n", "    - Client Name\n    - Client Gender\n")


def test_the_layout_a_filter_rewrite_writes_is_kept() -> None:
    """The old insert put "    - x" between "  measures:" and "  - Total Sales"."""
    out = _insert_into_query(REWRITTEN, "Average Sale", "measures")
    assert out == REWRITTEN.replace("  - Total Sales\n", "  - Total Sales\n  - Average Sale\n")


def test_an_inline_list_stays_inline() -> None:
    query = "select:\n  dimensions: [Product Name]\n  measures: [Total Sales]\n"
    out = _insert_into_query(query, "Average Sale", "measures")
    assert out == "select:\n  dimensions: [Product Name]\n  measures: [Total Sales, Average Sale]\n"


def test_comments_and_quoting_are_kept() -> None:
    query = (
        "# yaml-language-server: $schema=../schema/query-schema.json\n"
        "select:\n"
        "  measures:\n"
        "    - 'Total Sales'  # the headline number\n"
    )
    out = _insert_into_query(query, "Average Sale", "measures")
    assert out.startswith("# yaml-language-server: $schema=../schema/query-schema.json\n")
    assert "    - 'Total Sales'  # the headline number\n" in out
    assert yaml.safe_load(out) == {"select": {"measures": ["Total Sales", "Average Sale"]}}


def test_an_artefact_already_selected_is_not_added_again() -> None:
    assert _insert_into_query(STARTER, "Total Sales", "measures") == STARTER
    assert _insert_into_query(STARTER, "Client Name", "dimensions") == STARTER


def test_a_metric_listed_under_metrics_is_not_added_to_measures() -> None:
    query = "select:\n  measures:\n    - Total Sales\n  metrics:\n    - Return Rate\n"
    assert _insert_into_query(query, "Return Rate", "measures") == query


def test_an_empty_key_is_filled_where_it_stands() -> None:
    """The starter query of a model without examples."""
    out = _insert_into_query("select:\n  dimensions:\n  measures:\n", "Total Sales", "measures")
    assert out == "select:\n  dimensions:\n  measures:\n    - Total Sales\n"


def test_a_missing_dimensions_key_goes_before_measures() -> None:
    out = _insert_into_query("select:\n  measures:\n    - Total Sales\n", "Region", "dimensions")
    assert list(yaml.safe_load(out)["select"]) == ["dimensions", "measures"]


def test_an_empty_query_gets_a_select() -> None:
    out = _insert_into_query("", "Total Sales", "measures")
    assert out == "select:\n  measures:\n    - Total Sales\n"


def test_a_query_wrapper_is_followed() -> None:
    query = "query:\n  select:\n    measures:\n      - Total Sales\n"
    out = _insert_into_query(query, "Average Sale", "measures")
    assert yaml.safe_load(out) == {
        "query": {"select": {"measures": ["Total Sales", "Average Sale"]}}
    }


def test_a_column_adds_a_filter_to_fill_in() -> None:
    out = _insert_into_query(STARTER, "Product Name", "where")
    where = yaml.safe_load(out)["where"]
    assert where[-1] == {"field": "Product Name", "op": "equals", "value": None}
    assert "  - field: Product Name\n    op: equals\n    value:\n" in out


def test_a_new_where_goes_before_order_by_and_limit() -> None:
    query = "select:\n  measures:\n    - Total Sales\norderBy:\n  - field: Total Sales\nlimit: 10\n"
    out = _insert_into_query(query, "Region", "where")
    assert list(yaml.safe_load(out)) == ["select", "where", "orderBy", "limit"]


def test_a_query_that_does_not_parse_is_refused_with_its_line() -> None:
    """The reported case: "measures:" one space deeper than "dimensions:"."""
    broken = STARTER.replace("\n  measures:", "\n   measures:")
    with pytest.raises(QueryInsertError, match="does not parse at line 5"):
        _insert_into_query(broken, "Average Sale", "measures")


def test_a_bare_dash_line_is_refused_not_crashed_on() -> None:
    """ruamel's indent guesser reads past the end of "  -" under a top-level key."""
    with pytest.raises(QueryInsertError, match="does not parse"):
        _insert_into_query("select:\n  -\n", "Average Sale", "measures")


@pytest.mark.parametrize(
    ("query", "problem"),
    [
        ("- just a list\n", "not a mapping"),
        ("select: Total Sales\n", "'select' is not a mapping"),
        ("select:\n  measures: Total Sales\n", "'select.measures' is not a list"),
        ("where: Region\n", "'where' is not a list"),
    ],
)
def test_a_query_of_the_wrong_shape_is_refused(query: str, problem: str) -> None:
    section = "where" if query.startswith("where") else "measures"
    with pytest.raises(QueryInsertError, match=problem):
        _insert_into_query(query, "Region", section)
