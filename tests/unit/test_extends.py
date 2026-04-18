"""Unit tests for extends merger."""

from __future__ import annotations

from pathlib import Path

import pytest

from orionbelt.parser.merger import ExtendsMerger, MergeError

FIXTURES = Path(__file__).parent.parent / "fixtures" / "extends"


@pytest.fixture
def merger() -> ExtendsMerger:
    return ExtendsMerger()


class TestExtendsFromFiles:
    """Test file-based extends merging."""

    def test_single_extend_adds_dimensions(self, merger: ExtendsMerger) -> None:
        raw = {
            "version": 1.0,
            "extends": ["date_dims.yaml"],
            "dataObjects": {"Orders": {"code": "ORDERS", "columns": {}}},
        }
        merged, warnings = merger.merge_from_files(raw, FIXTURES)
        assert "Order Date" in merged["dimensions"]
        assert "Order Year" in merged["dimensions"]
        assert merged["dataObjects"] == {"Orders": {"code": "ORDERS", "columns": {}}}

    def test_multiple_extends_merge_in_order(self, merger: ExtendsMerger) -> None:
        raw = {
            "version": 1.0,
            "extends": ["date_dims.yaml", "standard_kpis.yaml"],
            "dataObjects": {"Orders": {"code": "ORDERS", "columns": {}}},
        }
        merged, warnings = merger.merge_from_files(raw, FIXTURES)
        assert "Order Date" in merged["dimensions"]
        assert "Revenue" in merged["measures"]
        assert "Order Count" in merged["measures"]

    def test_same_name_override_produces_warning(self, merger: ExtendsMerger) -> None:
        raw = {
            "version": 1.0,
            "extends": ["standard_kpis.yaml", "override.yaml"],
            "dataObjects": {"Orders": {"code": "ORDERS", "columns": {}}},
        }
        merged, warnings = merger.merge_from_files(raw, FIXTURES)
        assert any("Revenue" in w and "override" in w for w in warnings)
        assert merged["measures"]["Revenue"]["expression"] == "{[Orders].[Price]}"

    def test_extend_with_data_objects_raises(self, merger: ExtendsMerger) -> None:
        raw = {
            "version": 1.0,
            "extends": ["invalid_with_data_objects.yaml"],
            "dataObjects": {"Orders": {"code": "ORDERS", "columns": {}}},
        }
        with pytest.raises(MergeError, match="must not contain") as exc_info:
            merger.merge_from_files(raw, FIXTURES)
        assert exc_info.value.code == "EXTENDS_CONTAINS_DATA_OBJECTS"

    def test_circular_extends_raises(self, merger: ExtendsMerger) -> None:
        raw = {
            "version": 1.0,
            "extends": ["circular_a.yaml"],
            "dataObjects": {"Orders": {"code": "ORDERS", "columns": {}}},
        }
        with pytest.raises(MergeError, match="Circular reference") as exc_info:
            merger.merge_from_files(raw, FIXTURES)
        assert exc_info.value.code == "CIRCULAR_EXTENDS"

    def test_nested_extends(self, merger: ExtendsMerger) -> None:
        raw = {
            "version": 1.0,
            "extends": ["nested_base.yaml"],
            "dataObjects": {"Orders": {"code": "ORDERS", "columns": {}}},
        }
        merged, warnings = merger.merge_from_files(raw, FIXTURES)
        assert "Price Dim" in merged["dimensions"]
        assert "Avg Price" in merged["measures"]

    def test_file_not_found_raises(self, merger: ExtendsMerger) -> None:
        raw = {
            "version": 1.0,
            "extends": ["nonexistent.yaml"],
            "dataObjects": {"Orders": {"code": "ORDERS", "columns": {}}},
        }
        with pytest.raises(MergeError, match="not found") as exc_info:
            merger.merge_from_files(raw, FIXTURES)
        assert exc_info.value.code == "EXTENDS_FILE_NOT_FOUND"

    def test_main_model_data_objects_preserved(self, merger: ExtendsMerger) -> None:
        raw = {
            "version": 1.0,
            "extends": ["date_dims.yaml"],
            "dataObjects": {"MyTable": {"code": "MY_TABLE", "columns": {"id": "ID"}}},
            "dimensions": {"Local": {"dataObject": "MyTable", "column": "id"}},
        }
        merged, _ = merger.merge_from_files(raw, FIXTURES)
        assert "MyTable" in merged["dataObjects"]
        assert "Local" in merged["dimensions"]
        assert "Order Date" in merged["dimensions"]

    def test_extends_sources_metadata(self, merger: ExtendsMerger) -> None:
        raw = {
            "version": 1.0,
            "extends": ["date_dims.yaml", "standard_kpis.yaml"],
            "dataObjects": {},
        }
        merged, _ = merger.merge_from_files(raw, FIXTURES)
        assert merged["_extends_sources"] == ["date_dims.yaml", "standard_kpis.yaml"]

    def test_full_main_model_file(self, merger: ExtendsMerger) -> None:
        """Integration: load the main_model.yaml fixture which has extends."""
        import yaml

        main_path = FIXTURES / "main_model.yaml"
        raw = yaml.safe_load(main_path.read_text()) or {}
        merged, warnings = merger.merge_from_files(raw, FIXTURES)

        assert "Order Date" in merged["dimensions"]
        assert "Order Year" in merged["dimensions"]
        assert "Country" in merged["dimensions"]
        assert "Revenue" in merged["measures"]
        assert "Order Count" in merged["measures"]
        assert "Revenue per Order" in merged["metrics"]
        assert "Customers" in merged["dataObjects"]
        assert "extends" not in merged


class TestExtendsFromStrings:
    """Test inline YAML string extends merging."""

    def test_inline_extends(self, merger: ExtendsMerger) -> None:
        raw = {
            "version": 1.0,
            "dataObjects": {"Orders": {"code": "ORDERS", "columns": {}}},
        }
        ext_yaml = """
dimensions:
  Inline Dim:
    dataObject: Orders
    column: Price
    resultType: float
"""
        merged, warnings = merger.merge_from_strings(raw, extend_yamls=[ext_yaml])
        assert "Inline Dim" in merged["dimensions"]

    def test_inline_extend_with_data_objects_raises(self, merger: ExtendsMerger) -> None:
        raw = {"version": 1.0, "dataObjects": {}}
        ext_yaml = """
dataObjects:
  Bad:
    code: BAD
    columns: {}
"""
        with pytest.raises(MergeError, match="must not contain") as exc_info:
            merger.merge_from_strings(raw, extend_yamls=[ext_yaml])
        assert exc_info.value.code == "EXTENDS_CONTAINS_DATA_OBJECTS"


class TestMaxDepthExceeded:
    """Test max depth enforcement for nested extends."""

    def test_max_depth_exceeded(self, merger: ExtendsMerger, tmp_path: Path) -> None:
        for i in range(7):
            content = "version: 1.0\n"
            if i < 6:
                content += f"extends:\n  - level{i + 1}.yaml\n"
            content += f"dimensions:\n  Dim {i}:\n    dataObject: X\n    column: c\n"
            (tmp_path / f"level{i}.yaml").write_text(content)

        raw = {"version": 1.0, "extends": ["level0.yaml"], "dataObjects": {}}
        with pytest.raises(MergeError, match="maximum depth") as exc_info:
            merger.merge_from_files(raw, tmp_path)
        assert exc_info.value.code == "EXTENDS_MAX_DEPTH_EXCEEDED"
