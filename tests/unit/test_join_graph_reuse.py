"""Join graphs are built once per model and path overrides inside a compilation.

One compilation asks for the same ``JoinGraph`` several times. Inside
``reuse_join_graphs`` :meth:`JoinGraph.of` returns the graph already built for
the same model instance and the same effective ``(source, target) -> pathName``
overrides; ``CompilationPipeline.compile`` runs inside one. Nothing may change:
every compiled result, and every error, must equal the one without reuse.
"""

from __future__ import annotations

import itertools
import threading
from unittest import mock

import pytest
import yaml

from orionbelt.compiler import graph as graph_module
from orionbelt.compiler.graph import JoinGraph, reuse_join_graphs
from orionbelt.compiler.pipeline import CompilationPipeline, CompilationResult
from orionbelt.models.query import QueryObject, QuerySelect, UsePathName
from orionbelt.models.semantic import SemanticModel
from orionbelt.service.model_store import ModelStore
from tests.unit.test_dimension_roles import _load as load_role_model
from tests.unit.test_effective_measures_reuse import DIALECTS, TPCDS_MODEL, TPCDS_QUERIES
from tests.unit.test_nested_planner import (
    MODEL_YAML as NESTED_YAML,
)
from tests.unit.test_nested_planner import (
    _load as load_nested_model,
)
from tests.unit.test_nested_planner import (
    _with_a_dimension_behind_the_array,
    _with_second_fact,
)

SUPPORT = [UsePathName(source="Orders", target="Employees", path_name="support")]


@pytest.fixture(scope="module")
def tpcds() -> SemanticModel:
    store = ModelStore()
    loaded = store.load_model(TPCDS_MODEL.read_text(), dedup=False)
    return store.get_model(loaded.model_id)


def _outcome(
    pipeline: CompilationPipeline, query: QueryObject, model: SemanticModel, dialect: str
) -> CompilationResult | tuple[str, str]:
    try:
        return pipeline.compile(query, model, dialect)
    except Exception as exc:  # noqa: BLE001 - the error itself is compared
        return (type(exc).__name__, str(exc))


def _assert_parity(model: SemanticModel, queries: list[QueryObject], dialect: str) -> None:
    reuse = CompilationPipeline()
    rebuild = CompilationPipeline(reuse_measures=False, reuse_graphs=False)
    for query in queries:
        assert _outcome(reuse, query, model, dialect) == _outcome(rebuild, query, model, dialect), (
            query.model_dump(exclude_none=True)
        )


def _sweep(model: SemanticModel, **query_kwargs: object) -> list[QueryObject]:
    """Every single dimension and pair of dimensions, against each measure."""
    dims = sorted(model.dimensions)
    groups = [[d] for d in dims] + [list(p) for p in itertools.combinations(dims, 2)]
    return [
        QueryObject(select=QuerySelect(dimensions=g, measures=[m]), **query_kwargs)
        for g in groups
        for m in sorted(model.effective_measures)
    ]


class TestScope:
    def test_outside_a_scope_every_call_builds(self) -> None:
        model = load_role_model()
        assert JoinGraph.of(model) is not JoinGraph.of(model)

    def test_same_model_and_overrides_reuse_one_graph(self) -> None:
        model = load_role_model()
        with reuse_join_graphs():
            assert JoinGraph.of(model) is JoinGraph.of(model, None)
            assert JoinGraph.of(model, SUPPORT) is JoinGraph.of(model, list(SUPPORT))

    def test_different_overrides_get_different_graphs(self) -> None:
        model = load_role_model()
        with reuse_join_graphs():
            assert JoinGraph.of(model) is not JoinGraph.of(model, SUPPORT)

    def test_duplicate_overrides_resolve_as_before(self) -> None:
        """The last override for a pair wins, as ``path_overrides`` resolves it."""
        model = load_role_model()
        sales = UsePathName(source="Orders", target="Employees", path_name="sales")
        with reuse_join_graphs():
            assert JoinGraph.of(model, [sales, *SUPPORT]) is JoinGraph.of(model, SUPPORT)

    def test_each_model_instance_has_its_own_graph(self) -> None:
        with reuse_join_graphs():
            assert JoinGraph.of(load_role_model()) is not JoinGraph.of(load_role_model())

    def test_scope_ends_with_the_block(self) -> None:
        model = load_role_model()
        with reuse_join_graphs():
            inside = JoinGraph.of(model)
        assert JoinGraph.of(model) is not inside

    def test_nested_block_reuses_the_outer_one(self) -> None:
        model = load_role_model()
        with reuse_join_graphs():
            outer = JoinGraph.of(model)
            with reuse_join_graphs():
                assert JoinGraph.of(model) is outer

    def test_scope_is_not_shared_across_threads(self) -> None:
        model = load_role_model()
        seen: list[JoinGraph] = []
        with reuse_join_graphs():
            inside = JoinGraph.of(model)
            thread = threading.Thread(target=lambda: seen.append(JoinGraph.of(model)))
            thread.start()
            thread.join()
        assert seen[0] is not inside


class TestCompilation:
    def test_repeated_graph_requests_build_once(self) -> None:
        model = load_role_model()
        query = QueryObject(select=QuerySelect(dimensions=["Sales Employee"], measures=["Revenue"]))
        with mock.patch.object(
            graph_module.JoinGraph,
            "_build",
            autospec=True,
            side_effect=graph_module.JoinGraph._build,
        ) as build:
            CompilationPipeline(reuse_graphs=False).compile(query, model, "duckdb")
            without = build.call_count
            build.reset_mock()
            CompilationPipeline().compile(query, model, "duckdb")
        assert build.call_count < without

    def test_role_model(self) -> None:
        model = load_role_model()
        _assert_parity(model, _sweep(model), "duckdb")

    def test_role_model_with_a_secondary_path(self) -> None:
        model = load_role_model()
        _assert_parity(model, _sweep(model, use_path_names=SUPPORT), "duckdb")

    @pytest.mark.parametrize(
        "variant",
        [
            pytest.param(lambda y: y, id="nested"),
            pytest.param(_with_second_fact, id="nested-cfl"),
            pytest.param(_with_a_dimension_behind_the_array, id="nested-onward-join"),
        ],
    )
    @pytest.mark.parametrize("dialect", ["duckdb", "snowflake", "bigquery"])
    def test_nested_models(self, variant: object, dialect: str) -> None:
        assert callable(variant)
        model = load_nested_model(variant(NESTED_YAML))
        _assert_parity(model, _sweep(model), dialect)

    def test_sales_model(self, sales_model: SemanticModel) -> None:
        _assert_parity(sales_model, _sweep(sales_model), "duckdb")

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_tpcds_results_equal_without_graph_reuse(
        self, tpcds: SemanticModel, dialect: str
    ) -> None:
        reuse = CompilationPipeline()
        rebuild = CompilationPipeline(reuse_graphs=False)
        for path in TPCDS_QUERIES:
            query = QueryObject.model_validate(yaml.safe_load(path.read_text()))
            assert reuse.compile(query, tpcds, dialect) == rebuild.compile(query, tpcds, dialect), (
                path.stem
            )
