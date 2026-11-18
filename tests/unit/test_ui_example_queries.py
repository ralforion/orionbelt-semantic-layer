"""The playground's "Example queries" dropdown, filled from a model's ``examples:``."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("gradio", reason="gradio required by the UI handlers")

from orionbelt.ui.handlers import (  # noqa: E402
    _insert_into_query,
    example_picker_update,
    load_example_query,
    model_example_choices,
    starting_query,
)

_DEMO = Path(__file__).resolve().parents[2] / "examples" / "orionbelt_1_commerce.yaml"

MODEL = """\
version: 1.0
examples:
  - name: top_clients_by_sales
    description: The ten best clients.
    query:
      select: {dimensions: [Client Name], measures: [Total Sales]}
      limit: 10
  - name: broken_entry
    description: No query, so it cannot fill the box.
  - description: No name either.
    query: {select: {dimensions: [X]}}
"""


def test_choices_label_each_named_example_in_order() -> None:
    assert model_example_choices(MODEL) == [("Top clients by sales", "top_clients_by_sales")]


def test_dropdown_is_hidden_for_a_model_without_examples() -> None:
    update = example_picker_update("version: 1.0\ndimensions: {}\n")
    assert update["visible"] is False
    assert update["choices"] == []


def test_dropdown_is_shown_and_cleared_for_a_model_with_examples() -> None:
    update = example_picker_update(MODEL)
    assert update["visible"] is True
    assert update["value"] is None


def test_unparsable_model_hides_the_dropdown() -> None:
    assert example_picker_update("dimensions: [unclosed")["visible"] is False


def test_pick_replaces_the_query_box_and_clears_the_pick() -> None:
    query_yaml, picker = load_example_query("top_clients_by_sales", MODEL, "select: {}\n")
    assert yaml.safe_load(query_yaml) == {
        "select": {"dimensions": ["Client Name"], "measures": ["Total Sales"]},
        "limit": 10,
    }
    assert picker["value"] is None


def test_unknown_pick_keeps_the_current_query() -> None:
    current = "select: {dimensions: [Keep Me]}\n"
    assert load_example_query("missing", MODEL, current)[0] == current
    assert load_example_query(None, MODEL, current)[0] == current


def test_demo_model_offers_its_examples() -> None:
    names = [name for _, name in model_example_choices(_DEMO.read_text())]
    assert "client_vs_supplier_country" in names
    assert len(names) >= 5


def test_pick_outside_the_server_choices_reaches_the_handler() -> None:
    """After a server restart the choices are the startup model's.

    A pick from the model the page holds must still reach
    :func:`load_example_query`, which checks it against that model; Gradio's
    own choice validation would reject it first.
    """
    from unittest.mock import patch

    from orionbelt.ui import app as ui_app

    with (
        patch.object(ui_app, "_fetch_settings", return_value={"_unreachable": True}),
        patch.object(ui_app, "_fetch_dialects", return_value=["duckdb"]),
    ):
        blocks = ui_app.create_blocks(default_api_url="http://example.invalid")
    picker = next(
        c for c in blocks.blocks.values() if getattr(c, "label", None) == "Example queries"
    )
    assert picker.preprocess("top_clients_by_sales_of_a_custom_model") == (
        "top_clients_by_sales_of_a_custom_model"
    )


def test_a_chosen_model_starts_with_its_first_example() -> None:
    """Picking a curated model in the "Model" dropdown replaces the query box."""
    assert yaml.safe_load(starting_query(MODEL)) == {
        "select": {"dimensions": ["Client Name"], "measures": ["Total Sales"]},
        "limit": 10,
    }


def test_a_model_without_examples_starts_with_an_empty_select() -> None:
    """One the dimension and measure pickers can fill in."""
    query = starting_query("version: 1.0\ndimensions: {}\n")
    query = _insert_into_query(query, "Customer Country", "dimensions")
    query = _insert_into_query(query, "Revenue", "measures")
    assert yaml.safe_load(query) == {
        "select": {"dimensions": ["Customer Country"], "measures": ["Revenue"]}
    }
