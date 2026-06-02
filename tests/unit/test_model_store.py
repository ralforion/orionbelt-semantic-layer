"""Unit tests for the ModelStore service layer."""

from __future__ import annotations

import pytest

from orionbelt.service.model_store import ModelStore

# Re-use the sample YAML from conftest
from tests.conftest import SAMPLE_MODEL_YAML


@pytest.fixture
def store() -> ModelStore:
    return ModelStore()


# ---------------------------------------------------------------------------
# load_model
# ---------------------------------------------------------------------------


class TestLoadModel:
    def test_load_valid_model(self, store: ModelStore) -> None:
        result = store.load_model(SAMPLE_MODEL_YAML)
        assert len(result.model_id) == 8
        assert result.data_objects == 2
        assert result.dimensions == 1
        assert result.measures == 3
        assert result.metrics == 2

    def test_load_invalid_yaml_raises(self, store: ModelStore) -> None:
        with pytest.raises(ValueError, match="validation failed"):
            store.load_model("key: [unclosed")

    def test_load_model_with_bad_reference_raises(self, store: ModelStore) -> None:
        bad = """\
version: 1.0
dataObjects:
  T:
    code: T
    database: DB
    schema: S
    columns:
      F1:
        code: COL
        abstractType: string
dimensions:
  D1:
    dataObject: NONEXISTENT
    column: F1
    resultType: string
"""
        with pytest.raises(ValueError, match="validation failed"):
            store.load_model(bad)


# ---------------------------------------------------------------------------
# get_model / remove_model
# ---------------------------------------------------------------------------


class TestGetAndRemove:
    def test_get_model(self, store: ModelStore) -> None:
        result = store.load_model(SAMPLE_MODEL_YAML)
        model = store.get_model(result.model_id)
        assert "Customers" in model.data_objects
        assert "Orders" in model.data_objects

    def test_get_missing_raises(self, store: ModelStore) -> None:
        with pytest.raises(KeyError, match="No model loaded"):
            store.get_model("nonexist")

    def test_get_raw_returns_obml_dict(self, store: ModelStore) -> None:
        result = store.load_model(SAMPLE_MODEL_YAML)
        raw = store.get_raw(result.model_id)
        assert isinstance(raw, dict)
        assert "dataObjects" in raw

    def test_get_raw_missing_raises(self, store: ModelStore) -> None:
        with pytest.raises(KeyError, match="No model loaded"):
            store.get_raw("nonexist")

    def test_get_raw_returns_isolated_copy(self, store: ModelStore) -> None:
        # Mutating the returned dict must not corrupt the store's internal
        # raw, so later exports / inherits stay intact.
        result = store.load_model(SAMPLE_MODEL_YAML)
        raw = store.get_raw(result.model_id)
        raw["dataObjects"] = "corrupted"
        raw["injected"] = True
        fresh = store.get_raw(result.model_id)
        assert fresh["dataObjects"] != "corrupted"
        assert "injected" not in fresh

    def test_remove_model(self, store: ModelStore) -> None:
        result = store.load_model(SAMPLE_MODEL_YAML)
        store.remove_model(result.model_id)
        with pytest.raises(KeyError):
            store.get_model(result.model_id)

    def test_remove_missing_raises(self, store: ModelStore) -> None:
        with pytest.raises(KeyError, match="No model loaded"):
            store.remove_model("nonexist")


# ---------------------------------------------------------------------------
# describe
# ---------------------------------------------------------------------------


class TestDescribe:
    def test_describe(self, store: ModelStore) -> None:
        result = store.load_model(SAMPLE_MODEL_YAML)
        desc = store.describe(result.model_id)

        assert desc.model_id == result.model_id
        assert len(desc.data_objects) == 2
        assert len(desc.dimensions) == 1
        assert len(desc.measures) == 3
        assert len(desc.metrics) == 2

        obj_labels = {o.label for o in desc.data_objects}
        assert obj_labels == {"Customers", "Orders"}

        orders = next(o for o in desc.data_objects if o.label == "Orders")
        assert "Customers" in orders.join_targets

        dim = desc.dimensions[0]
        assert dim.name == "Customer Country"
        assert dim.data_object == "Customers"
        assert dim.column == "Country"

    def test_describe_missing_raises(self, store: ModelStore) -> None:
        with pytest.raises(KeyError):
            store.describe("nonexist")


# ---------------------------------------------------------------------------
# list_models
# ---------------------------------------------------------------------------


class TestListModels:
    def test_empty(self, store: ModelStore) -> None:
        assert store.list_models() == []

    def test_list_after_load(self, store: ModelStore) -> None:
        # dedup=False so each load creates a new model — needed to verify list semantics.
        r1 = store.load_model(SAMPLE_MODEL_YAML, dedup=False)
        r2 = store.load_model(SAMPLE_MODEL_YAML, dedup=False)
        assert r1.model_id != r2.model_id
        models = store.list_models()
        assert len(models) == 2
        ids = {m.model_id for m in models}
        assert r1.model_id in ids
        assert r2.model_id in ids


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_valid_model(self, store: ModelStore) -> None:
        summary = store.validate(SAMPLE_MODEL_YAML)
        assert summary.valid is True
        assert summary.errors == []

    def test_invalid_yaml(self, store: ModelStore) -> None:
        summary = store.validate("key: [unclosed")
        assert summary.valid is False
        assert len(summary.errors) >= 1
        assert summary.errors[0].code == "YAML_PARSE_ERROR"

    def test_invalid_reference(self, store: ModelStore) -> None:
        bad = """\
version: 1.0
dataObjects:
  T:
    code: T
    database: DB
    schema: S
    columns:
      F1:
        code: COL
        abstractType: string
dimensions:
  D1:
    dataObject: MISSING
    column: F1
    resultType: string
"""
        summary = store.validate(bad)
        assert summary.valid is False
        assert any(e.code for e in summary.errors)


# ---------------------------------------------------------------------------
# compile_query
# ---------------------------------------------------------------------------


class TestDedup:
    """Cover the dedup behavior added in v2.2.0 (PLAN_model_load_dedup.md)."""

    def test_same_yaml_reuses_model_id(self, store: ModelStore) -> None:
        r1 = store.load_model(SAMPLE_MODEL_YAML)
        r2 = store.load_model(SAMPLE_MODEL_YAML)
        assert r1.model_id == r2.model_id
        assert r1.model_load == "fresh"
        assert r2.model_load == "reused"
        # Counts on the reused result come from the cached summary.
        assert r2.data_objects == r1.data_objects
        assert r2.dimensions == r1.dimensions
        assert r2.measures == r1.measures
        assert r2.metrics == r1.metrics

    def test_dedup_false_forces_fresh_load(self, store: ModelStore) -> None:
        r1 = store.load_model(SAMPLE_MODEL_YAML)
        r2 = store.load_model(SAMPLE_MODEL_YAML, dedup=False)
        assert r1.model_id != r2.model_id
        assert r2.model_load == "fresh"

    def test_trailing_whitespace_normalized(self, store: ModelStore) -> None:
        r1 = store.load_model(SAMPLE_MODEL_YAML)
        r2 = store.load_model("\n\n" + SAMPLE_MODEL_YAML + "\n\n")
        assert r1.model_id == r2.model_id

    def test_different_yaml_loads_separately(self, store: ModelStore) -> None:
        # Add a comment — different bytes, different hash, fresh load.
        r1 = store.load_model(SAMPLE_MODEL_YAML)
        r2 = store.load_model("# variant\n" + SAMPLE_MODEL_YAML)
        assert r1.model_id != r2.model_id
        assert r2.model_load == "fresh"

    def test_remove_clears_index(self, store: ModelStore) -> None:
        r1 = store.load_model(SAMPLE_MODEL_YAML)
        store.remove_model(r1.model_id)
        # No stale index entry — same content loads fresh.
        r2 = store.load_model(SAMPLE_MODEL_YAML)
        assert r2.model_id != r1.model_id
        assert r2.model_load == "fresh"

    def test_dedup_skipped_when_extends_provided(self, store: ModelStore) -> None:
        # extends/inherits make the effective content depend on input the
        # YAML bytes don't capture — dedup must not apply.
        r1 = store.load_model(SAMPLE_MODEL_YAML)
        r2 = store.load_model(SAMPLE_MODEL_YAML, extends_yaml=[])
        # Empty extends list still bypasses dedup eligibility check
        # (see load_model: `not extends_yaml` is True for [], so dedup runs).
        # Verify the inverse: a non-empty extends bypasses dedup.
        del r2  # unused
        r3 = store.load_model(SAMPLE_MODEL_YAML, extends_yaml=["version: 1.0\n"])
        assert r3.model_id != r1.model_id
        assert r3.model_load == "fresh"


class TestCompileQuery:
    def test_compile_simple(self, store: ModelStore) -> None:
        from orionbelt.models.query import QueryObject, QuerySelect

        result = store.load_model(SAMPLE_MODEL_YAML)
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Total Revenue"],
            )
        )
        comp = store.compile_query(result.model_id, query, "postgres")
        assert comp.sql
        assert comp.dialect == "postgres"
        assert "Customer Country" in comp.resolved.dimensions
        assert "Total Revenue" in comp.resolved.measures

    def test_compile_missing_model_raises(self, store: ModelStore) -> None:
        from orionbelt.models.query import QueryObject, QuerySelect

        query = QueryObject(select=QuerySelect(dimensions=["X"], measures=["Y"]))
        with pytest.raises(KeyError):
            store.compile_query("nonexist", query, "postgres")
