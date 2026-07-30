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


_DISCONNECTED_MODEL_YAML = """\
version: 1.0

dataObjects:
  Products:
    code: PRODUCTS
    database: WH
    schema: PUBLIC
    columns:
      Product ID:
        code: ID
        abstractType: string
      Category:
        code: CATEGORY
        abstractType: string

  Sales:
    code: SALES
    database: WH
    schema: PUBLIC
    columns:
      Sale ID:
        code: ID
        abstractType: string
      Sale Product ID:
        code: PRODUCT_ID
        abstractType: string
      Region:
        code: REGION
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: Products
        columnsFrom: [Sale Product ID]
        columnsTo: [Product ID]

  Suppliers:
    code: SUPPLIERS
    database: WH
    schema: PUBLIC
    columns:
      Supplier ID:
        code: ID
        abstractType: string
      Supplier Name:
        code: NAME
        abstractType: string

dimensions:
  Category:
    dataObject: Products
    column: Category
    resultType: string
  Region:
    dataObject: Sales
    column: Region
    resultType: string
  Supplier Name:
    dataObject: Suppliers
    column: Supplier Name
    resultType: string
"""


class TestCommonRootDisconnected:
    """``find_common_root`` over objects that span disconnected components.

    No single node reaches all of them, so the search falls back to the
    undirected Steiner centre. That fallback builds its candidate set from
    *pairwise* shortest paths and therefore skips unreachable pairs — so a
    candidate need not reach every required node. Scoring it used to assume
    otherwise and raised ``NetworkXNoPath`` out of the compiler.
    """

    @staticmethod
    def _graph() -> JoinGraph:
        loader = TrackedLoader()
        raw, source_map = loader.load_string(_DISCONNECTED_MODEL_YAML)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors
        return JoinGraph(model)

    def test_returns_a_node_instead_of_raising(self) -> None:
        """Two connected objects plus one disconnected: the crash shape.

        Products/Sales populate the Steiner set; Suppliers is unreachable from
        either, which is what the scoring pass could not handle.
        """
        root = self._graph().find_common_root({"Products", "Sales", "Suppliers"})
        assert root in {"Products", "Sales", "Suppliers"}

    def test_two_disconnected_objects_alone(self) -> None:
        """No pair is connected, so the Steiner set stays empty."""
        root = self._graph().find_common_root({"Products", "Suppliers"})
        assert root in {"Products", "Suppliers"}

    def test_connected_objects_still_resolve_to_the_real_root(self) -> None:
        """The fallback must not disturb the ordinary directed-ancestor answer."""
        assert self._graph().find_common_root({"Products", "Sales"}) == "Sales"
