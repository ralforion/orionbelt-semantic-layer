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
