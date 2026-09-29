"""Effective measures are computed once per model instance inside a compilation.

``SemanticModel.effective_measures`` rebuilds the synthesized counts on every
access; one compilation reads it hundreds of times. ``reuse_effective_measures``
keeps the first result per model instance for the duration of a block, and
``CompilationPipeline.compile`` runs inside one. Nothing may change: the
compiled result with reuse must equal the result without it.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest import mock

import pytest
import yaml

import orionbelt.models.synthesis as synthesis
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.roles import expand_role_objects
from orionbelt.models.semantic import SemanticModel, reuse_effective_measures
from orionbelt.service.model_store import ModelStore
from tests.unit.test_dimension_roles import _load as load_role_model

ROOT = Path(__file__).resolve().parents[2]
TPCDS_MODEL = ROOT / "examples" / "tpcds.obml.yml"
TPCDS_QUERIES = sorted((ROOT / "examples" / "tpcds_queries").glob("Q*.yml"))

DIALECTS = [
    "bigquery",
    "clickhouse",
    "databricks",
    "dremio",
    "duckdb",
    "mysql",
    "postgres",
    "snowflake",
]


@pytest.fixture(scope="module")
def tpcds_model() -> SemanticModel:
    store = ModelStore()
    loaded = store.load_model(TPCDS_MODEL.read_text(), dedup=False)
    return store.get_model(loaded.model_id)


def _counting_synthesis() -> mock._patch[mock.MagicMock]:
    return mock.patch.object(
        synthesis, "synthesize_count_measures", wraps=synthesis.synthesize_count_measures
    )


class TestScope:
    def test_outside_a_scope_every_access_is_fresh(self, sales_model: SemanticModel) -> None:
        assert sales_model.effective_measures is not sales_model.effective_measures

    def test_inside_a_scope_the_first_result_is_reused(self, sales_model: SemanticModel) -> None:
        with _counting_synthesis() as synthesize, reuse_effective_measures():
            first = sales_model.effective_measures
            assert sales_model.effective_measures is first
        assert synthesize.call_count == 1

    def test_scope_ends_with_the_block(self, sales_model: SemanticModel) -> None:
        with reuse_effective_measures():
            inside = sales_model.effective_measures
        assert sales_model.effective_measures is not inside

    def test_nested_block_reuses_the_outer_one(self, sales_model: SemanticModel) -> None:
        with reuse_effective_measures():
            outer = sales_model.effective_measures
            with reuse_effective_measures():
                assert sales_model.effective_measures is outer
            assert sales_model.effective_measures is outer

    def test_each_model_instance_has_its_own_entry(self) -> None:
        model = load_role_model()
        expanded = expand_role_objects(model)
        assert expanded is not model
        with reuse_effective_measures():
            assert model.effective_measures == load_role_model().effective_measures
            assert expanded.effective_measures is not model.effective_measures
            assert set(expanded.effective_measures) == set(
                expand_role_objects(load_role_model()).effective_measures
            )

    def test_scope_is_not_shared_across_threads(self, sales_model: SemanticModel) -> None:
        seen: list[object] = []
        with reuse_effective_measures():
            inside = sales_model.effective_measures
            thread = threading.Thread(target=lambda: seen.append(sales_model.effective_measures))
            thread.start()
            thread.join()
        assert seen[0] is not inside
        assert seen[0] == inside


class TestCompilation:
    def test_synthesis_runs_once_per_model_instance(self, tpcds_model: SemanticModel) -> None:
        query = QueryObject.model_validate(yaml.safe_load(TPCDS_QUERIES[0].read_text()))
        with _counting_synthesis() as synthesize:
            CompilationPipeline().compile(query, tpcds_model, "duckdb")
        assert synthesize.call_count == 1

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_tpcds_results_equal_without_reuse(
        self, tpcds_model: SemanticModel, dialect: str
    ) -> None:
        reuse = CompilationPipeline()
        rebuild = CompilationPipeline(reuse_measures=False)
        for path in TPCDS_QUERIES:
            query = QueryObject.model_validate(yaml.safe_load(path.read_text()))
            assert reuse.compile(query, tpcds_model, dialect) == rebuild.compile(
                query, tpcds_model, dialect
            ), path.stem

    @pytest.mark.parametrize(
        "dims",
        [
            ["Sales Employee"],
            ["Sales Employee", "Support Employee"],
            ["Support Employee ID", "Support Employee Display"],
            ["Employee Name"],
        ],
    )
    def test_role_model_results_equal_without_reuse(self, dims: list[str]) -> None:
        model = load_role_model()
        query = QueryObject(select=QuerySelect(dimensions=dims, measures=["Revenue"]))
        assert CompilationPipeline().compile(query, model, "duckdb") == CompilationPipeline(
            reuse_measures=False
        ).compile(query, model, "duckdb")

    def test_synthesized_count_results_equal_without_reuse(
        self, sales_model: SemanticModel
    ) -> None:
        counts = sorted(set(sales_model.effective_measures) - set(sales_model.measures))
        assert counts, "the sales fixture should synthesize at least one count"
        query = QueryObject(select=QuerySelect(dimensions=[], measures=counts))
        assert CompilationPipeline().compile(query, sales_model, "duckdb") == CompilationPipeline(
            reuse_measures=False
        ).compile(query, sales_model, "duckdb")
