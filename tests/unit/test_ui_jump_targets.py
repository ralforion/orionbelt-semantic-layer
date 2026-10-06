"""The model editor's "Jump to" navigator (``model_jump_targets``)."""

from __future__ import annotations

from pathlib import Path

import pytest

gr = pytest.importorskip("gradio")

from orionbelt.ui.handlers import model_jump_targets  # noqa: E402

MODEL = """\
version: 1.0
settings:
  defaultDialect: duckdb
metrics:
  Average Sale:
    expression: "{[Total Sales]} / {[Sales Count]}"
rules:
  Electronics Sale:
    expression: x
  High Return Rate:
    expression: y
examples:
  - name: top_clients
    query: {}
  - description: name second
    name: "monthly sales"
    query: {}
ontology:
  prefixes: {}
"""


def _choices(text: str) -> list[tuple[str, str]]:
    update = model_jump_targets(text)
    return list(update["choices"])


def test_sections_after_metrics_are_not_filed_under_metrics() -> None:
    """Rules and examples showed as ``metrics / ...`` and ``metrics / - name``."""
    labels = [label for label, _ in _choices(MODEL)]
    assert labels == [
        "version",
        "settings",
        "metrics",
        "metrics / Average Sale",
        "rules",
        "rules / Electronics Sale",
        "rules / High Return Rate",
        "examples",
        "examples / top_clients",
        "examples / monthly sales",
        "ontology",
    ]


def test_each_entry_points_at_its_line() -> None:
    lines = MODEL.split("\n")
    for label, line in _choices(MODEL):
        text = lines[int(line) - 1]
        if label == "examples / monthly sales":
            # The item starts on its "- " line, before the name key.
            assert text.strip().startswith("- description")
        else:
            assert label.split(" / ")[-1] in text


def test_the_bundled_model_lists_every_named_child() -> None:
    import yaml

    text = (Path(__file__).parents[2] / "examples" / "orionbelt_1_commerce.yaml").read_text()
    model = yaml.safe_load(text)
    labels = [label for label, _ in _choices(text)]
    for section in ("dataObjects", "dimensions", "measures", "metrics", "rules", "examples"):
        assert sum(label.startswith(f"{section} / ") for label in labels) == len(model[section])
    assert not [label for label in labels if "- " in label]


def test_unindented_lists_keep_their_examples() -> None:
    """``yaml.safe_dump`` writes ``- name:`` at column 0 (review of #517)."""
    import yaml

    text = (Path(__file__).parents[2] / "examples" / "orionbelt_1_commerce.yaml").read_text()
    model = yaml.safe_load(text)
    dumped = yaml.safe_dump(model, sort_keys=False)
    assert "\n- name:" in dumped
    labels = [label for label, _ in _choices(dumped)]
    assert sum(label.startswith("examples / ") for label in labels) == len(model["examples"])
    lines = dumped.split("\n")
    for label, line in _choices(dumped):
        if label.startswith("examples / "):
            assert lines[int(line) - 1].startswith("- name: " + label.split(" / ", 1)[1])


def test_a_key_starting_with_a_hyphen_keeps_its_target() -> None:
    """``-Profit`` is a valid dimension name, not a list marker (review of #517)."""
    text = "dimensions:\n  -Profit:\n    dataObject: Sales\n  Plain:\n    dataObject: Sales\n"
    assert _choices(text) == [
        ("dimensions", "1"),
        ("dimensions / -Profit", "2"),
        ("dimensions / Plain", "4"),
    ]


def test_yaml_that_does_not_parse_leaves_the_choices_alone() -> None:
    """Mid-edit, the dropdown keeps its last good targets instead of emptying."""
    update = model_jump_targets("metrics:\n  Average Sale: [unclosed\n")
    assert "choices" not in update
