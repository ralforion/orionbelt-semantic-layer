"""Unit tests for inherits merger."""

from __future__ import annotations

from pathlib import Path

import pytest

from orionbelt.parser.merger import ExtendsMerger, MergeError

FIXTURES = Path(__file__).parent.parent / "fixtures" / "extends"


@pytest.fixture
def merger() -> ExtendsMerger:
    return ExtendsMerger()


class TestInheritsFromFiles:
    """Test file-based inherits merging."""

    def test_inherits_gets_parent_definitions(self, merger: ExtendsMerger) -> None:
        import yaml

        raw = yaml.safe_load((FIXTURES / "inherited_view.yaml").read_text()) or {}
        merged, warnings = merger.merge_from_files(raw, FIXTURES)

        assert "Customers" in merged["dataObjects"]
        assert "Orders" in merged["dataObjects"]
        assert "Country" in merged["dimensions"]
        assert "Order Date" in merged["dimensions"]
        assert "Revenue" in merged["measures"]
        assert "Order Count" in merged["measures"]
        assert "Revenue per Order" in merged["metrics"]
        assert "inherits" not in merged

    def test_child_overrides_parent_measure(self, merger: ExtendsMerger) -> None:
        parent = {
            "version": 1.0,
            "dataObjects": {
                "T": {"code": "T", "columns": {"c": {"code": "C", "abstractType": "string"}}}
            },
            "dimensions": {"D": {"dataObject": "T", "column": "c"}},
            "measures": {"M": {"aggregation": "sum", "expression": "old"}},
        }
        child = {
            "version": 1.0,
            "measures": {"M": {"aggregation": "avg", "expression": "new"}},
        }
        merged, warnings = merger.merge_from_strings(child, inherits_raw=parent)
        assert merged["measures"]["M"]["expression"] == "new"
        assert any("Measure" in w and "'M'" in w for w in warnings)

    def test_child_adds_static_filter(self, merger: ExtendsMerger) -> None:
        import yaml

        raw = yaml.safe_load((FIXTURES / "inherited_view.yaml").read_text()) or {}
        merged, _ = merger.merge_from_files(raw, FIXTURES)

        assert len(merged["filters"]) == 1
        assert merged["filters"][0]["column"] == "Region"
        assert merged["filters"][0]["value"] == "EMEA"

    def test_parent_and_child_filters_combine(self, merger: ExtendsMerger) -> None:
        parent = {
            "version": 1.0,
            "dataObjects": {"T": {"code": "T", "columns": {}}},
            "filters": [
                {"dataObject": "T", "column": "status", "operator": "equals", "value": "active"}
            ],
        }
        child = {
            "version": 1.0,
            "filters": [
                {"dataObject": "T", "column": "region", "operator": "equals", "value": "EU"}
            ],
        }
        merged, _ = merger.merge_from_strings(child, inherits_raw=parent)
        assert len(merged["filters"]) == 2
        assert merged["filters"][0]["column"] == "status"
        assert merged["filters"][1]["column"] == "region"

    def test_child_adds_new_measure(self, merger: ExtendsMerger) -> None:
        import yaml

        raw = yaml.safe_load((FIXTURES / "inherited_view.yaml").read_text()) or {}
        merged, _ = merger.merge_from_files(raw, FIXTURES)

        assert "EMEA Margin" in merged["measures"]
        assert "Revenue" in merged["measures"]

    def test_inherits_source_metadata(self, merger: ExtendsMerger) -> None:
        import yaml

        raw = yaml.safe_load((FIXTURES / "inherited_view.yaml").read_text()) or {}
        merged, _ = merger.merge_from_files(raw, FIXTURES)
        assert merged["_inherits_source"] == "parent_model.yaml"

    def test_parent_not_found_raises(self, merger: ExtendsMerger) -> None:
        raw = {"version": 1.0, "inherits": "nonexistent.yaml"}
        with pytest.raises(MergeError, match="not found") as exc_info:
            merger.merge_from_files(raw, FIXTURES)
        assert exc_info.value.code == "PARENT_MODEL_NOT_FOUND"


class TestInheritsValidation:
    """Test validation rules for inherits."""

    def test_parent_with_extends_raises(self, merger: ExtendsMerger) -> None:
        parent = {
            "version": 1.0,
            "extends": ["something.yaml"],
            "dataObjects": {"T": {"code": "T", "columns": {}}},
        }
        child = {"version": 1.0}
        with pytest.raises(MergeError, match="must not use 'extends'") as exc_info:
            merger.merge_from_strings(child, inherits_raw=parent)
        assert exc_info.value.code == "PARENT_HAS_EXTENDS"

    def test_parent_with_inherits_raises(self, merger: ExtendsMerger) -> None:
        parent = {
            "version": 1.0,
            "inherits": "something.yaml",
            "dataObjects": {"T": {"code": "T", "columns": {}}},
        }
        child = {"version": 1.0}
        with pytest.raises(MergeError, match="must not use 'inherits'") as exc_info:
            merger.merge_from_strings(child, inherits_raw=parent)
        assert exc_info.value.code == "PARENT_HAS_INHERITS"

    def test_child_with_data_objects_raises(self, merger: ExtendsMerger) -> None:
        parent = {
            "version": 1.0,
            "dataObjects": {"T": {"code": "T", "columns": {}}},
        }
        child = {
            "version": 1.0,
            "dataObjects": {"U": {"code": "U", "columns": {}}},
        }
        with pytest.raises(MergeError, match="must not define") as exc_info:
            merger.merge_from_strings(child, inherits_raw=parent)
        assert exc_info.value.code == "INHERITS_CONTAINS_DATA_OBJECTS"

    def test_child_with_extends_raises(self, merger: ExtendsMerger) -> None:
        raw = {
            "version": 1.0,
            "extends": ["something.yaml"],
            "inherits": "parent.yaml",
        }
        with pytest.raises(MergeError, match="cannot have both") as exc_info:
            merger.merge_from_strings(raw)
        assert exc_info.value.code == "INVALID_EXTENDS_INHERITS_COMBINATION"


class TestInheritsFromStrings:
    """Test inline inherits merging (API mode)."""

    def test_inherits_from_raw_dict(self, merger: ExtendsMerger) -> None:
        parent = {
            "version": 1.0,
            "dataObjects": {
                "Orders": {
                    "code": "ORDERS",
                    "columns": {"Price": {"code": "PRICE", "abstractType": "float"}},
                }
            },
            "dimensions": {
                "Price": {"dataObject": "Orders", "column": "Price", "resultType": "float"}
            },
            "measures": {"Total": {"aggregation": "sum", "expression": "{[Orders].[Price]}"}},
        }
        child = {
            "version": 1.0,
            "dimensions": {
                "Custom": {"dataObject": "Orders", "column": "Price", "resultType": "float"}
            },
        }
        merged, warnings = merger.merge_from_strings(child, inherits_raw=parent)
        assert "Orders" in merged["dataObjects"]
        assert "Price" in merged["dimensions"]
        assert "Custom" in merged["dimensions"]
        assert "Total" in merged["measures"]
