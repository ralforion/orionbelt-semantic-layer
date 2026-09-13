"""The SPARQL example gallery runs against the commerce demo's graph.

The gallery feeds the Gradio UI's SPARQL tab, and the demo model is what
the public playground loads, so every entry must parse and find something
there: a gallery whose examples come back empty teaches nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rdflib import Graph

from orionbelt.obsl.exporter import export_obsl
from orionbelt.obsl.sparql import execute_sparql
from orionbelt.obsl.sparql_examples import (
    EXAMPLE_TITLES,
    SPARQL_EXAMPLES,
    SparqlExample,
    example_query,
)
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

_MODEL = Path(__file__).resolve().parents[2] / "examples" / "orionbelt_1_commerce.yaml"


@pytest.fixture(scope="module")
def graph() -> Graph:
    raw, sm = TrackedLoader().load(_MODEL)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.errors == []
    return export_obsl(model, "orionbelt_1_commerce")


def test_titles_are_unique_and_looked_up() -> None:
    assert len(set(EXAMPLE_TITLES)) == len(EXAMPLE_TITLES)
    for example in SPARQL_EXAMPLES:
        assert example_query(example.title) == example.query
    assert example_query("no such example") == ""


@pytest.mark.parametrize("example", SPARQL_EXAMPLES, ids=lambda e: e.title)
def test_every_example_finds_something_in_the_demo(graph: Graph, example: SparqlExample) -> None:
    result = execute_sparql(graph, example.query)
    if result.type == "ask":
        assert result.boolean is True
    else:
        assert result.type == "select"
        assert result.results, "a gallery example must find something in the demo"
        assert result.variables
