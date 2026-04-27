"""Tests for the join graph."""

from __future__ import annotations

from orionbelt.compiler.graph import JoinGraph
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from tests.conftest import SAMPLE_MODEL_YAML


def _load_model() -> SemanticModel:
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, source_map = loader.load_string(SAMPLE_MODEL_YAML)
    model, result = resolver.resolve(raw, source_map)
    assert result.valid
    return model


class TestJoinGraph:
    def test_graph_nodes(self) -> None:
        model = _load_model()
        graph = JoinGraph(model)
        assert graph._graph.number_of_nodes() == 2  # Orders, Customers

    def test_graph_edges(self) -> None:
        model = _load_model()
        graph = JoinGraph(model)
        assert graph._graph.number_of_edges() == 1  # Orders -> Customers

    def test_find_join_path(self) -> None:
        model = _load_model()
        graph = JoinGraph(model)
        steps = graph.find_join_path({"Orders"}, {"Orders", "Customers"})
        assert len(steps) == 1
        assert steps[0].from_object == "Orders"
        assert steps[0].to_object == "Customers"

    def test_find_join_path_same_object(self) -> None:
        model = _load_model()
        graph = JoinGraph(model)
        steps = graph.find_join_path({"Orders"}, {"Orders"})
        assert len(steps) == 0

    def test_build_join_condition(self) -> None:
        model = _load_model()
        graph = JoinGraph(model)
        steps = graph.find_join_path({"Orders"}, {"Orders", "Customers"})
        assert len(steps) == 1
        condition = graph.build_join_condition(steps[0])
        assert condition is not None

    def test_find_join_path_forward_not_reversed(self) -> None:
        """Forward traversal (same direction as declared) sets reversed=False."""
        model = _load_model()
        graph = JoinGraph(model)
        steps = graph.find_join_path({"Orders"}, {"Orders", "Customers"})
        assert len(steps) == 1
        assert steps[0].reversed is False

    def test_find_join_path_refuses_to_reverse_many_to_one(self) -> None:
        """Reverse traversal of many-to-one is forbidden (would inflate row counts).

        Walking the Orders→Customers (many-to-one) join from Customers back to
        Orders is not a valid traversal direction, so ``find_join_path`` returns
        no steps.  Callers (the resolver) detect the unreachable required
        object and raise UNREACHABLE_REQUIRED_OBJECT.
        """
        model = _load_model()
        graph = JoinGraph(model)
        steps = graph.find_join_path({"Customers"}, {"Customers", "Orders"})
        assert steps == []

    def test_no_cycles_in_simple_model(self) -> None:
        model = _load_model()
        graph = JoinGraph(model)
        cycles = graph.detect_cycles()
        assert len(cycles) == 0
